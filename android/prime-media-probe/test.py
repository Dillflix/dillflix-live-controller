#!/usr/bin/env python3
"""Run host fault tests of the production core using Java 17+ and ECJ (no Android device)."""
import argparse, hashlib, pathlib, subprocess, urllib.request
ROOT=pathlib.Path(__file__).resolve().parent
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--cache',type=pathlib.Path,default=ROOT/'.test-cache')
p.add_argument('--ecj',type=pathlib.Path,default=ROOT/'.build/ecj.jar')
a=p.parse_args(); a.cache.mkdir(parents=True,exist_ok=True)
jar=a.cache/'json-20240303.jar'
if not jar.exists():
    jar.write_bytes(urllib.request.urlopen('https://repo.maven.apache.org/maven2/org/json/json/20240303/json-20240303.jar',timeout=30).read())
assert hashlib.sha256(jar.read_bytes()).hexdigest()=='3cf6cd6892e32e2b4c1c39e0f52f5248a2f5b37646fdfbb79a66b46b618414ed'
classes=a.cache/'classes'; classes.mkdir(exist_ok=True)
subprocess.run(['java','-jar',str(a.ecj),'-source','1.8','-target','1.8','-proc:none','-cp',str(jar),'-d',str(classes),str(ROOT/'ProbeSupport.java'),str(ROOT/'AsyncJournal.java'),str(ROOT/'tests/CoreTest.java')],check=True)
subprocess.run(['java','-ea','-cp',str(classes)+':'+str(jar),'dev.tvprobe.mediasession.CoreTest'],check=True)
