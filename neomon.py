#!/usr/bin/env python3
"""NeoMon launcher – run this directly: python neomon.py"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from neomon.__main__ import main

if __name__ == "__main__":
    main()
