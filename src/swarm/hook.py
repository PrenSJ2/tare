"""The entrypoint Claude Code invokes: `swarm-hook <EventName>`, payload on stdin.

Contract: exit 0, write nothing to stdout or stderr, whatever happens. Claude
Code may interpret hook output, and a non-zero exit or a traceback would put
swarm's failure into the operator's session -- the one thing this project must
never do.
"""

import json
import sys


def main() -> None:
    try:
        if len(sys.argv) < 2:
            return
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        from swarm.emit import emit

        emit(sys.argv[1], payload)
    except Exception:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
