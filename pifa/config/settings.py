"""Centralized application defaults for the layered MPC demo."""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Package names mirror the project layout shown in the reference IDE.
PACKAGE_LAYERS = (
    "config",
    "constants",
    "data_objects",
    "inputs",
    "outputs",
    "problem_solver",
    "service",
    "utils",
)
