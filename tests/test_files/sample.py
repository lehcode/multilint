import os
import sys

# Test Python file with intentional issues

# Flake8 style issues - missing newlines between functions
def foo():
    print("foo")
def bar():
    print("bar")

# Black formatting - missing space after comma
data=[1,2,3]

# Hardcoded secret (should be flagged by security scan)
PASSWORD = "test_password_123"

# Pylint would catch missing docstrings
class MyClass:
    def method(self):
        pass
