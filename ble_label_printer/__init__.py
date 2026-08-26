"""BLE label printer plugin for InvenTree.

This package provides a label printing plugin that renders labels to a
monochrome bitmap and generates a self-contained HTML page which uses the
browser Web Bluetooth API to send the label data to a BLE thermal printer.
"""

from .ble_label_printer import BLELabelPrinter

__all__ = ['BLELabelPrinter']
