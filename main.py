from __future__ import annotations

from djtube.app import app


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8098)


if __name__ == "__main__":
    main()
