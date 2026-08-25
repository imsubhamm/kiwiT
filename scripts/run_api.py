from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "kiwit.api:app", host=os.getenv("KIWIT_API_HOST", "127.0.0.1"),
        port=int(os.getenv("KIWIT_API_PORT", "8000")), proxy_headers=False,
    )


if __name__ == "__main__":
    main()
