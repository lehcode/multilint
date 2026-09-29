#!/bin/bash

# Test bash script with intentional issues

# ShellCheck violations - SC2086 (word splitting)
echo $VAR
echo $@

# Bad practice - SC2155 (declare and use)
declare -x SECRET="password123"
echo $SECRET

# Dangerous eval with variable
eval "$CMD"

# Bad indentation
if [ -d "/tmp" ]; then
	echo "tmp exists"
fi
