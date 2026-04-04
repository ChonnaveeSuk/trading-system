# trading-system/strategy/src/bridge/__init__.py
"""
gRPC bridge client — Python side.

Sends SignalResult objects to the Rust execution engine via gRPC.
The Rust engine runs the risk check and submits to the paper broker.
"""
