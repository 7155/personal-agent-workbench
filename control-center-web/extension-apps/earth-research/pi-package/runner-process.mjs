import { spawn } from 'node:child_process';

/** Execution failures are transport-independent and safe to persist in run receipts. */
export class RunnerError extends Error {
  constructor(code, message) { super(message); this.name = 'RunnerError'; this.code = code; }
}

export function throwIfAborted(signal) {
  if (signal?.aborted) throw new RunnerError('execution_cancelled', 'Execution was cancelled. No further step was started.');
}

export function executionFailure(error) {
  const code = error?.code || 'runner_failed';
  return {
    status: code === 'execution_cancelled' ? 'cancelled' : 'failed',
    code,
    error: error instanceof Error ? error.message : String(error),
    outputs: [],
  };
}

/**
 * No shell; bounded output; cancellation settles only after the child closes.
 * POSIX children get their own process group so a Python child cannot outlive
 * a cancelled job merely by keeping a pipe open. This is not an OS sandbox.
 */
export function runProcess({ executable, args = [], cwd, input, signal,
  timeoutMs = 300_000, graceMs = 1_000, maxOutputBytes = 32 * 1024 * 1024, env = process.env }) {
  throwIfAborted(signal);
  for (const [name, value] of Object.entries({ timeoutMs, graceMs, maxOutputBytes })) {
    if (!Number.isSafeInteger(value) || value < 1) throw new TypeError(`${name} must be a positive integer.`);
  }
  if (typeof executable !== 'string' || !executable || !Array.isArray(args) || args.some(arg => typeof arg !== 'string')) {
    throw new TypeError('A process executable and string arguments are required.');
  }
  if (input !== undefined && typeof input !== 'string') throw new TypeError('Process input must be serialized text.');
  return new Promise((resolve, reject) => {
    const grouped = process.platform !== 'win32';
    const child = spawn(executable, args, { cwd, env, shell: false, windowsHide: true,
      detached: grouped, stdio: ['pipe', 'pipe', 'pipe'] });
    const stdout = [], stderr = [];
    let bytes = 0, stderrBytes = 0, stopped, done = false, escalation;
    const kill = kind => {
      if (!child.pid) return;
      try { if (grouped) process.kill(-child.pid, kind); else child.kill(kind); }
      catch (error) { if (error?.code !== 'ESRCH') child.kill(kind); }
    };
    const stop = error => {
      if (done || stopped) return;
      stopped = error;
      kill('SIGTERM');
      escalation = setTimeout(() => kill('SIGKILL'), graceMs);
    };
    const abort = () => stop(new RunnerError('execution_cancelled', 'Execution was cancelled. Partial outputs were not accepted.'));
    const timeout = setTimeout(() => stop(new RunnerError('runner_timeout', `Execution exceeded ${timeoutMs} ms. Partial outputs were not accepted.`)), timeoutMs);
    const finish = (error, result) => {
      if (done) return;
      done = true; clearTimeout(timeout); clearTimeout(escalation);
      signal?.removeEventListener('abort', abort);
      error ? reject(error) : resolve(result);
    };
    const capture = (chunk, isError) => {
      bytes += chunk.length;
      if (bytes > maxOutputBytes) {
        stop(new RunnerError('runner_output_limit', `Runner output exceeded ${maxOutputBytes} bytes.`)); return;
      }
      if (stopped) return;
      if (!isError) stdout.push(chunk);
      // Keep a bounded diagnostic tail; it is not automatically exposed to a model.
      else { stderr.push(chunk); stderrBytes += chunk.length;
        while (stderr.length > 1 && stderrBytes > 8_192) stderrBytes -= stderr.shift().length;
      }
    };
    child.stdout.on('data', chunk => capture(chunk, false));
    child.stderr.on('data', chunk => capture(chunk, true));
    child.stdin.on('error', error => {
      if (error.code !== 'EPIPE') stop(new RunnerError('runner_stdin_failed', 'Could not send the request to the runner.'));
    });
    child.on('error', error => {
      kill('SIGKILL');
      finish(stopped || new RunnerError('runner_unavailable', `Could not start the configured runner (${error.code || 'spawn_error'}).`));
    });
    child.on('close', (exitCode, exitSignal) => {
      // A cooperative parent may close before a stubborn descendant with
      // detached stdio. Do not clear escalation while leaving that group alive.
      if (stopped && grouped) kill('SIGKILL');
      finish(stopped, {
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).subarray(-8_192).toString('utf8'), exitCode, exitSignal,
      });
    });
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort(); // Handles cancellation between preflight and listener installation.
    child.stdin.end(input);
  });
}

export async function runJSONProcess(options) {
  const output = await runProcess(options);
  let receipt;
  for (const line of output.stdout.trim().split(/\r?\n/).reverse()) {
    if (!line.trim().startsWith('{')) continue;
    try { receipt = JSON.parse(line); break; } catch { /* Python libraries may log first. */ }
  }
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt) || typeof receipt.status !== 'string') {
    throw new RunnerError('runner_invalid_receipt', 'Runner returned no structured status receipt.');
  }
  // A printed success must never hide an abnormal process termination.
  if (output.exitCode !== 0 && !['failed', 'cancelled'].includes(receipt.status)) {
    throw new RunnerError('runner_exit_failed', `Runner exited abnormally (${output.exitCode ?? output.exitSignal ?? 'unknown'}).`);
  }
  return receipt;
}
