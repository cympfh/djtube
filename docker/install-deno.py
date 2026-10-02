import hashlib
import io
import platform
import urllib.request
import zipfile
from pathlib import Path

version = "2.9.7"
sha256 = {
    "x86_64": "c6527f24f4b16031d3ae4fa9f658d5f11534c8d84ce7dc8502420280919c3490",
    "aarch64": "c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf",
}
arch = {
    "x86_64": "x86_64-unknown-linux-gnu",
    "aarch64": "aarch64-unknown-linux-gnu",
}
machine = platform.machine()
digest = sha256[machine]
url = f"https://github.com/denoland/deno/releases/download/v{version}/deno-{arch[machine]}.zip"
data = urllib.request.urlopen(url, timeout=180).read()
if hashlib.sha256(data).hexdigest() != digest:
    raise SystemExit("deno checksum mismatch")
binary = Path("/opt/djtube/deno")
binary.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    member = next(name for name in archive.namelist() if Path(name).name == "deno")
    binary.write_bytes(archive.read(member))
binary.chmod(0o755)
