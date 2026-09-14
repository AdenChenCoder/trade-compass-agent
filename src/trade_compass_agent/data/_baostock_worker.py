"""One Baostock request per process: its SDK uses a process-global socket.

The parent owns the wall-clock deadline and kills/reaps this process on timeout.
No application state is written here and SDK cleanup cannot delay the caller.
"""

import contextlib
import io
import json
import socket
import sys


def main() -> None:
    request = json.load(sys.stdin)
    # This is an isolated process, never the service's global socket default.
    socket.setdefaulttimeout(request["timeout"])
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import baostock as bs

            login = bs.login()
            if login.error_code != "0":
                raise RuntimeError(login.error_msg)
            result = bs.query_history_k_data_plus(
                request["symbol"], "date,open,high,low,close,volume,amount,turn",
                start_date=request["start_date"], end_date=request["end_date"],
                frequency="d", adjustflag="2",
            )
            rows = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            if result.error_code != "0":
                raise RuntimeError(result.error_msg)
        response = {"rows": rows}
    except Exception as exc:
        response = {"error": str(exc) or type(exc).__name__}
    # Process exit closes the SDK socket; logout itself can block on the server.
    print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
