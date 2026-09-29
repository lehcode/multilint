#!/bin/bash

# Test shell script with intentional issues

# Bad syntax - unclosed quote
echo "hello

# Dangerous patterns
chmod 777 /tmp/test
eval $USER_command

# Missing variables not quoted
echo $VAR

# Bad indentation (tabs)
if [ -f "/tmp/test" ]; then
	echo "found"
fi
