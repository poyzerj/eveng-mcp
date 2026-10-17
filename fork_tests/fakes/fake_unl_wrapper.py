"""Stand-in for /opt/unetlab/wrappers/unl_wrapper used by fake_ssh's shell mode.
Takes ~1s per call (like a slow export), logs its args, exits 16 for node ids in
FAKE_EXPORT_FAIL (comma list), else 0."""
import os, sys, time
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
with open(os.environ.get("FAKE_WRAPPER_LOG", "/tmp/fake_wrapper.log"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
time.sleep(float(os.environ.get("FAKE_EXPORT_SECONDS", "1")))
sys.exit(16 if args.get("-D") in os.environ.get("FAKE_EXPORT_FAIL", "").split(",") else 0)
