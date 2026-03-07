"""Entry point: python -m neomon"""
from neomon.collectors import Collector
from neomon.app import NeoMon


def main() -> None:
    col = Collector(interval=2.0)
    col.start()
    app = NeoMon(col)
    try:
        app.run()
    finally:
        col.stop()


if __name__ == "__main__":
    main()
