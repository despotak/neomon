"""Entry point: python -m neomon"""
from neomon.collectors import INTERVAL, Collector
from neomon.app import NeoMon


def main() -> None:
    col = Collector(interval=INTERVAL)
    col.start()
    app = NeoMon(col)
    try:
        app.run()
    finally:
        col.stop()


if __name__ == "__main__":
    main()
