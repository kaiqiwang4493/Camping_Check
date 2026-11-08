import os

from .monitor import main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        if os.getenv("GITHUB_ACTIONS") == "true":
            # Surface the cause in the Actions overview as well as the full log.
            message = f"{type(exc).__name__}: {exc}"
            message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Monitor scan failed::{message}", flush=True)
        raise

