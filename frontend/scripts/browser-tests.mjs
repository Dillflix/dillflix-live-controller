import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
  chmodSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { brotliDecompressSync } from "node:zlib";
import { createServer } from "node:net";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(frontend, "..");
const scratch = mkdtempSync(join(tmpdir(), "dillflix-browser-"));
let server;
let serverLog = "";

// The npm-distributed Linux browser also works where browser CDN downloads are unavailable.
// Avoid tar ownership restoration, which fails in some containers.
if (process.platform === "linux" && process.arch === "x64") {
  const cache = join(frontend, ".browser-cache");
  const bin = join(frontend, "node_modules/@sparticuz/chromium/bin");
  mkdirSync(cache, { recursive: true });
  if (!existsSync(join(cache, "chromium"))) {
    for (const name of ["chromium", "fonts.tar", "swiftshader.tar"]) {
      writeFileSync(
        join(cache, name),
        brotliDecompressSync(readFileSync(join(bin, name + ".br"))),
      );
      if (name.endsWith(".tar")) {
        const result = spawnSync("tar", [
          "--no-same-owner",
          "--no-same-permissions",
          "-xf",
          join(cache, name),
          "-C",
          cache,
        ]);
        if (result.status !== 0) throw new Error(result.stderr.toString());
        rmSync(join(cache, name));
      }
    }
    chmodSync(join(cache, "chromium"), 0o755);
  }
}

const run = (cmd, args, env = process.env) =>
  new Promise((done, reject) => {
    const child = spawn(cmd, args, {
      cwd: frontend,
      env,
      stdio: "inherit",
      // Windows command scripts require a shell (Node otherwise reports EINVAL).
      shell: process.platform === "win32" && cmd === "npm.cmd",
    });
    child.on("error", reject);
    child.on("exit", (code) =>
      code === 0 ? done() : reject(new Error(`${cmd} exited with ${code}`)),
    );
  });
try {
  await run(process.platform === "win32" ? "npm.cmd" : "npm", ["run", "build"]);
  let url = process.env.TEST_BASE_URL;
  if (!url) {
    const port = await new Promise((done, reject) => {
      const socket = createServer();
      socket.on("error", reject);
      socket.listen(0, "127.0.0.1", () => {
        const selected = socket.address().port;
        socket.close(() => done(selected));
      });
    });
    url = `http://127.0.0.1:${port}`;
    const python =
      process.env.CONTROLLER_TEST_PYTHON ||
      (process.platform === "win32" ? "python" : "python3");
    server = spawn(
      python,
      [
        "-m",
        "uvicorn",
        "controller.api:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
      ],
      {
        cwd: root,
        env: {
          ...process.env,
          CONTROLLER_MODE: "demo",
          CONTROLLER_DATABASE: join(scratch, "controller.sqlite3"),
          // Regression tests must never contact a developer's configured TV.
          SCREEN_ADB_SERIAL: "",
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    server.stdout.on("data", (data) => {
      serverLog += data;
    });
    server.stderr.on("data", (data) => {
      serverLog += data;
    });
    let launchError;
    server.on("error", (error) => {
      launchError = error;
    });
    let ready = false;
    for (let attempt = 0; attempt < 120; attempt++) {
      if (launchError) throw launchError;
      if (server.exitCode !== null)
        throw new Error(`API failed to start:\n${serverLog}`);
      try {
        ready = (await fetch(`${url}/api/health`)).ok;
      } catch {}
      if (ready) break;
      await new Promise((done) => setTimeout(done, 250));
    }
    if (!ready) throw new Error(`API did not become ready:\n${serverLog}`);
  }
  await run(
    process.execPath,
    ["node_modules/@playwright/test/cli.js", "test", ...process.argv.slice(2)],
    { ...process.env, TEST_BASE_URL: url },
  );
} catch (error) {
  console.error(error.message);
  if (serverLog) console.error(serverLog.slice(-6000));
  process.exitCode = 1;
} finally {
  if (server && server.exitCode === null) {
    server.kill("SIGTERM");
    await Promise.race([
      new Promise((done) => server.once("exit", done)),
      new Promise((done) => setTimeout(done, 4000)),
    ]);
    if (server.exitCode === null) server.kill("SIGKILL");
  }
  rmSync(scratch, { recursive: true, force: true });
}
