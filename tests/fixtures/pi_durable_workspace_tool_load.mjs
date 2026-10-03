import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Pure catalog/schema contract: no Harness, Provider, Tool execution, or service.
const dist = process.env.PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST;
if (!dist) throw new Error("PAW_PI_WORKSPACE_TOOL_TEST_HOST_DIST is required");
const { BackendToolRegistry } = await import(pathToFileURL(resolve(dist, "tool-bridge.js")).href);
const { loadBackendTools } = await import(pathToFileURL(resolve(dist, "discovery-tools.js")).href);
let source = "";
for await (const chunk of process.stdin) source += chunk;
const input = JSON.parse(source);

function inspect(manifests, names) {
  const registry = new BackendToolRegistry();
  registry.sync(manifests);
  const result = { catalog: registry.catalog().map((tool) => tool.name), loaded: {}, error: null };
  try {
    for (let offset = 0; offset < names.length; offset += 4) {
      for (const item of loadBackendTools(registry, { names: names.slice(offset, offset + 4) })) {
        result.loaded[item.tool.name] = item.result.tool;
      }
    }
  } catch (error) {
    result.error = String(error.message);
  }
  return result;
}

process.stdout.write(JSON.stringify({
  durable: inspect(input.durable, input.names),
  classic: inspect(input.classic, ["workspace_shell"]),
}));
