"""Launch the POLY alert deck: streams + paper engine + dashboard on port 18112."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from poly.server import main

if __name__ == "__main__":
    print("POLY ALERT DECK -> http://127.0.0.1:18112", flush=True)
    main()
