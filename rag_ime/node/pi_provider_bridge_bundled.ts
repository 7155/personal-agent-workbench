import { readFile } from "node:fs/promises";
import { createServer } from "node:net";
import { join } from "node:path";
import process from "node:process";

import { AuthStorage } from "rag-ime-pi-auth-storage";
import { ModelRuntime } from "rag-ime-pi-model-runtime";
import { openaiCodexOAuth } from "rag-ime-pi-openai-codex-oauth";

const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_MODELS_PER_PROVIDER = 120;
const PROVIDER_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

type RequestPayload = Record<string, unknown>;

function emit(value: Record<string, unknown>): void {
	process.stdout.write(`${JSON.stringify(value)}\n`);
}

function publicError(error: unknown): string {
	const raw = error instanceof Error ? error.message : String(error);
	return (
		raw
			.replace(/(?:sk|key|token|bearer)[-_A-Za-z0-9.]{8,}/gi, "[redacted]")
			.replace(/[\r\n\t]+/g, " ")
			.slice(0, 300) || "Pi provider operation failed"
	);
}

async function readRequest(): Promise<RequestPayload> {
	const chunks: Buffer[] = [];
	let size = 0;
	for await (const chunk of process.stdin) {
		const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
		size += buffer.length;
		if (size > MAX_REQUEST_BYTES) throw new Error("request is too large");
		chunks.push(buffer);
	}
	const request = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as unknown;
	if (!request || typeof request !== "object" || Array.isArray(request)) {
		throw new Error("request must be an object");
	}
	return request as RequestPayload;
}

function requiredString(request: RequestPayload, key: string, maximum = 4096): string {
	const value = typeof request[key] === "string" ? request[key].trim() : "";
	if (!value || value.length > maximum) throw new Error(`${key} is invalid`);
	return value;
}

function providerId(request: RequestPayload): string {
	const value = requiredString(request, "provider", 128);
	if (!PROVIDER_ID_PATTERN.test(value)) throw new Error("provider is invalid");
	return value;
}

async function configuredProviderIds(agentDir: string): Promise<Set<string>> {
	const ids = new Set<string>();
	try {
		const parsed = JSON.parse(await readFile(join(agentDir, "models.json"), "utf8")) as {
			providers?: unknown;
		};
		if (parsed.providers && typeof parsed.providers === "object" && !Array.isArray(parsed.providers)) {
			for (const id of Object.keys(parsed.providers)) ids.add(id);
		}
	} catch {
		// ModelRuntime reports configuration errors; discovery remains fail-open.
	}
	return ids;
}

async function loadPi(request: RequestPayload) {
	const agentDir = requiredString(request, "agentDir", 4096);
	const auth = AuthStorage.create(join(agentDir, "auth.json"));
	const runtime = await ModelRuntime.create({
		credentials: auth,
		modelsPath: join(agentDir, "models.json"),
		allowModelNetwork: false,
	});
	return { agentDir, auth, runtime };
}

function publicModelNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function publicModelCost(value: unknown): Record<string, unknown> | undefined {
	if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
	const cost = value as Record<string, unknown>;
	if (cost.tiers !== undefined && !Array.isArray(cost.tiers)) return undefined;
	const rates = (entry: unknown): Record<string, unknown> => {
		if (!entry || typeof entry !== "object" || Array.isArray(entry)) return { input: null };
		const fields = entry as Record<string, unknown>;
		return Object.fromEntries(["input", "output", "cacheRead", "cacheWrite"]
			.filter((key) => fields[key] !== undefined)
			// Invalid supplied prices stay explicitly unknown, never a free rate.
			.map((key) => [key, publicModelNumber(fields[key]) ?? null]));
	};
	return { ...rates(cost), ...(Array.isArray(cost.tiers) ? { tiers: cost.tiers.map(rates) } : {}) };
}

async function providerCatalog(
	auth: AuthStorage,
	runtime: ModelRuntime,
	configuredIds: ReadonlySet<string>,
): Promise<Record<string, unknown>[]> {
	const credentialInfo = await auth.list();
	const credentialTypes = new Map(credentialInfo.map((item) => [item.providerId, item.type]));
	const runtimeProviders = new Map(runtime.getProviders().map((provider) => [provider.id, provider]));
	const availableByProvider = new Map<string, ReturnType<ModelRuntime["getModels"]>>();
	for (const model of runtime.getAvailableSnapshot()) {
		const current = availableByProvider.get(model.provider) ?? [];
		availableByProvider.set(model.provider, [...current, model]);
	}
	const ids = new Set([...runtimeProviders.keys(), ...credentialTypes.keys(), ...configuredIds]);
	const providers: Record<string, unknown>[] = [];
	for (const id of [...ids].sort((left, right) => left.localeCompare(right))) {
		const provider = runtimeProviders.get(id);
		const models = runtime.getModels(id);
		const available = availableByProvider.get(id) ?? [];
		const status = runtime.getProviderAuthStatus(id);
		const credentialType = credentialTypes.get(id) ?? "";
		providers.push({
			id,
			name: provider?.name ?? id,
			auth: {
				configured: Boolean(status.configured || available.length),
				type: credentialType,
				source: status.source ?? "",
				sourceLabel: status.label ?? "",
				oauthSupported: Boolean(provider?.auth.oauth),
				oauthBrowserSupported: id === "openai-codex" && Boolean(provider?.auth.oauth),
				oauthDeviceCodeSupported: id === "openai-codex" && Boolean(provider?.auth.oauth),
				apiKeySupported: Boolean(provider?.auth.apiKey),
			},
			configuredInCatalog: configuredIds.has(id),
			modelCount: models.length,
			availableModelCount: available.length,
			availableModels: available.slice(0, MAX_MODELS_PER_PROVIDER).map((model) => ({
				id: model.id,
				name: model.name,
				provider: model.provider,
				api: model.api,
				contextWindow: publicModelNumber(model.contextWindow),
				maxTokens: publicModelNumber(model.maxTokens),
				// Project only public pricing fields, never custom URLs or headers.
				cost: publicModelCost(model.cost),
				reasoning: Boolean(model.reasoning),
				imageInput: Array.isArray(model.input) && model.input.includes("image"),
			})),
			modelsTruncated: available.length > MAX_MODELS_PER_PROVIDER,
		});
	}
	return providers;
}

async function catalog(request: RequestPayload): Promise<Record<string, unknown>> {
	const { agentDir, auth, runtime } = await loadPi(request);
	return {
		event: "result",
		ok: true,
		providers: await providerCatalog(auth, runtime, await configuredProviderIds(agentDir)),
		catalogError: runtime.getError() || "",
	};
}

async function knownProviderIds(request: RequestPayload, runtime: ModelRuntime): Promise<Set<string>> {
	const agentDir = requiredString(request, "agentDir", 4096);
	return new Set([
		...runtime.getProviders().map((provider) => provider.id),
		...(await configuredProviderIds(agentDir)),
	]);
}

async function setApiKey(request: RequestPayload): Promise<Record<string, unknown>> {
	const provider = providerId(request);
	const apiKey = requiredString(request, "apiKey", 16 * 1024);
	const { auth, runtime } = await loadPi(request);
	if (!(await knownProviderIds(request, runtime)).has(provider)) {
		throw new Error("provider is not in the Pi model catalog");
	}
	const beforeType = (await auth.read(provider))?.type ?? "";
	await auth.modify(provider, async () => ({ type: "api_key", key: apiKey }));
	return { event: "result", ok: true, provider, beforeType, authType: "api_key" };
}

async function logout(request: RequestPayload): Promise<Record<string, unknown>> {
	const provider = providerId(request);
	const { auth, runtime } = await loadPi(request);
	if (!(await knownProviderIds(request, runtime)).has(provider)) {
		throw new Error("provider is not in the Pi model catalog");
	}
	const beforeType = (await auth.read(provider))?.type ?? "";
	await auth.delete(provider);
	return { event: "result", ok: true, provider, beforeType, authType: "" };
}

async function assertBrowserCallbackAvailable(): Promise<void> {
	const host = process.env.PI_OAUTH_CALLBACK_HOST?.trim() || "127.0.0.1";
	await new Promise<void>((resolve, reject) => {
		const server = createServer();
		server.once("error", () => {
			reject(new Error("OpenAI Codex browser callback port 1455 is unavailable; use device-code fallback"));
		});
		server.listen(1455, host, () => {
			server.close((error) => {
				if (error) reject(error);
				else resolve();
			});
		});
	});
}

function waitForBrowserCallback(signal?: AbortSignal): Promise<string> {
	return new Promise((_resolve, reject) => {
		const aborted = () => reject(new Error("Browser callback prompt closed"));
		if (signal?.aborted) {
			aborted();
			return;
		}
		signal?.addEventListener("abort", aborted, { once: true });
	});
}

async function oauthLogin(request: RequestPayload): Promise<void> {
	const provider = providerId(request);
	if (provider !== "openai-codex") {
		throw new Error("OAuth login is unavailable for this provider");
	}
	const method = request.action === "oauth_browser" ? "browser" : "device_code";
	if (method === "browser") await assertBrowserCallbackAvailable();
	const agentDir = requiredString(request, "agentDir", 4096);
	const auth = AuthStorage.create(join(agentDir, "auth.json"));
	emit({ event: "state", state: "starting", provider });
	const credential = await openaiCodexOAuth.login({
		signal: new AbortController().signal,
		prompt: async (prompt) => {
			if (prompt.type === "select" && prompt.options.some((option) => option.id === method)) {
				return method;
			}
			if (method === "browser" && prompt.type === "manual_code") {
				return waitForBrowserCallback(prompt.signal);
			}
			throw new Error("interactive OAuth input is unavailable");
		},
		notify: (event) => emit({ ...event, event: event.type }),
	});
	await auth.modify(provider, async () => credential);
	emit({ event: "completed", ok: true, provider });
}

async function main(): Promise<void> {
	const request = await readRequest();
	switch (request.action) {
		case "catalog":
			emit(await catalog(request));
			return;
		case "set_api_key":
			emit(await setApiKey(request));
			return;
		case "logout":
			emit(await logout(request));
			return;
		case "oauth_device_code":
		case "oauth_browser":
			await oauthLogin(request);
			return;
		default:
			throw new Error("action is not allowed");
	}
}

main().catch((error) => {
	emit({ event: "failed", ok: false, error: publicError(error) });
	process.exitCode = 1;
});
