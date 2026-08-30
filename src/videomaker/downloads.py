from pathlib import Path

import httpx


def download_file(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    own_client = client is None
    if own_client:
        client = httpx.Client(follow_redirects=True, timeout=120.0)
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with open(tmp, "wb") as fh:
                fh.writelines(response.iter_bytes(256 * 1024))
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)
        if own_client:
            client.close()
