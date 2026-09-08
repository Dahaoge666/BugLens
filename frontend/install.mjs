#!/usr/bin/env node

import { cpSync, existsSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(fileURLToPath(new URL(".", import.meta.url)));
const defaultSource = resolve(frontendRoot, "dist");
const defaultOutput = resolve(frontendRoot, ".runtime", "frontend");

function argument(name, fallback) {
  const index = process.argv.indexOf(name);
  return index === -1 ? fallback : process.argv[index + 1];
}

function fail(message) {
  console.error(`Frontend installation failed: ${message}`);
  process.exit(1);
}

function commandAvailable(command) {
  const result = spawnSync(command, ["--version"], {
    stdio: "ignore",
    windowsHide: true,
  });
  return result.status === 0;
}

function packageRunner() {
  const pnpm = process.platform === "win32" ? "pnpm.cmd" : "pnpm";
  if (commandAvailable(pnpm)) return [pnpm];

  const corepack = process.platform === "win32" ? "corepack.cmd" : "corepack";
  if (commandAvailable(corepack)) return [corepack, "pnpm"];

  fail(
    "Node.js is available, but pnpm/Corepack is not. " +
      "Install Node.js 20+ with Corepack, then run this command again.",
  );
}

function run(command, args) {
  console.log("+", [command, ...args].join(" "));
  const result = spawnSync(command, args, {
    cwd: frontendRoot,
    stdio: "inherit",
    windowsHide: false,
  });
  if (result.error) fail(result.error.message);
  if (result.status !== 0) fail(`command exited with code ${result.status}`);
}

const source = resolve(argument("--source", defaultSource));
const output = resolve(argument("--output", defaultOutput));

const nodeMajor = Number(process.versions.node.split(".")[0]);
if (nodeMajor < 20) {
  fail(`Node.js 20 or newer is required; found ${process.versions.node}`);
}

if (!existsSync(resolve(frontendRoot, "package.json"))) {
  fail(`package.json was not found under ${frontendRoot}`);
}

if (!existsSync(resolve(source, "index.html"))) {
  const [runner, ...runnerArgs] = packageRunner();
  run(runner, [...runnerArgs, "install", "--frozen-lockfile"]);
  run(runner, [...runnerArgs, "run", "build"]);
}

if (!existsSync(resolve(source, "index.html"))) {
  fail(`frontend build did not create ${resolve(source, "index.html")}`);
}

if (source !== output) {
  rmSync(output, { recursive: true, force: true });
  cpSync(source, output, { recursive: true });
}

console.log(`Frontend is ready at ${output}`);
