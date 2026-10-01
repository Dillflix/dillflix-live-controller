#!/usr/bin/env python3
"""Build the standalone diagnostic APK on Linux with Python 3 and Java 17+."""

import argparse
import hashlib
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--cache", type=Path, default=ROOT / ".build")
parser.add_argument("--output", type=Path, default=ROOT / "dist" / "prime-media-probe.apk")
parser.add_argument("--keystore", type=Path, help="Existing private development key for a compatible update")
args = parser.parse_args()
CACHE = args.cache.resolve()
CACHE.mkdir(parents=True, exist_ok=True)
APK = args.output.resolve()
APK.parent.mkdir(parents=True, exist_ok=True)

ASSETS = [
    (
        "platform-30_r03.zip",
        "https://dl.google.com/android/repository/platform-30_r03.zip",
        "sha1",
        "e7c6280901dcfa511af098d67dd88c4dfcbc6ea2",
    ),
    (
        "build-tools_r30.0.3-linux.zip",
        "https://dl.google.com/android/repository/build-tools_r30.0.3-linux.zip",
        "sha1",
        "2076ea81b5a2fc298ef7bf85d666f496b928c7f1",
    ),
    (
        "ecj.jar",
        "https://repo.maven.apache.org/maven2/org/eclipse/jdt/ecj/3.37.0/ecj-3.37.0.jar",
        "sha256",
        "cde026ff966b48b5e5f148b6f041ceff3cf4f85cf75155f4ec0f40e4ee14b545",
    ),
]

for name, url, algorithm, checksum in ASSETS:
    path = CACHE / name
    if not path.exists():
        print("Downloading", name, flush=True)
        partial = path.with_suffix(path.suffix + ".partial")
        try:
            with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as output:
                shutil.copyfileobj(response, output, length=256 * 1024)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    if hashlib.new(algorithm, path.read_bytes()).hexdigest() != checksum:
        raise SystemExit("Checksum mismatch: " + str(path))
    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            archive.extractall(CACHE)

SDK = CACHE / "android-11"
ANDROID = SDK / "android.jar"
for name in ["aapt", "zipalign"]:
    (SDK / name).chmod(0o755)
CLASSES = CACHE / "classes"
DEX = CACHE / "dex"
for path in [CLASSES, DEX]:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir()


def run(*args):
    subprocess.run([str(a) for a in args], check=True)


run(
    "java",
    "-jar",
    CACHE / "ecj.jar",
    "-source",
    "1.8",
    "-target",
    "1.8",
    "-proc:none",
    "-bootclasspath",
    ANDROID,
    "-d",
    CLASSES,
    *sorted(ROOT.glob("*.java")),
)
run(
    "java",
    "-cp",
    SDK / "lib/d8.jar",
    "com.android.tools.r8.D8",
    "--min-api",
    "22",
    "--lib",
    ANDROID,
    "--output",
    DEX,
    *sorted(CLASSES.rglob("*.class")),
)
unsigned = CACHE / "unsigned.apk"
aligned = CACHE / "aligned.apk"
run(SDK / "aapt", "package", "-f", "-M", ROOT / "AndroidManifest.xml", "-I", ANDROID, "-F", unsigned)
with zipfile.ZipFile(unsigned, "a", compression=zipfile.ZIP_DEFLATED) as archive:
    archive.write(DEX / "classes.dex", "classes.dex")
run(SDK / "zipalign", "-f", "4", unsigned, aligned)
key = args.keystore.resolve() if args.keystore else CACHE / "development.p12"
if args.keystore and not key.is_file():
    raise SystemExit("Requested keystore does not exist")
if not key.exists():
    run(
        "keytool",
        "-genkeypair",
        "-keystore",
        key,
        "-storetype",
        "PKCS12",
        "-alias",
        "probe",
        "-storepass",
        "android",
        "-keypass",
        "android",
        "-keyalg",
        "RSA",
        "-keysize",
        "2048",
        "-validity",
        "3650",
        "-dname",
        "CN=Media Session Probe Development",
        "-noprompt",
    )
run(
    "java",
    "-jar",
    SDK / "lib/apksigner.jar",
    "sign",
    "--ks",
    key,
    "--ks-key-alias",
    "probe",
    "--ks-pass",
    "pass:android",
    "--key-pass",
    "pass:android",
    "--out",
    APK,
    aligned,
)
run("java", "-jar", SDK / "lib/apksigner.jar", "verify", "--verbose", "--print-certs", APK)
run(SDK / "aapt", "dump", "badging", APK)
print("Built:", APK)
print("SHA-256:", hashlib.sha256(APK.read_bytes()).hexdigest())
