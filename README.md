# shellinject.py
Spawn a reverse TCP in the context of another linux process

Largely based on [dlinject](https://github.com/DavidBuchanan314/dlinject/), but
with different stage2 shellcode. (Perhaps I should merge these codebases and/or
create a generic shellcode injection library?)

### TODO:

- Add support for other architectures (notably ARM).

- Don't rely on netcat for our reverse shell - we should just open a socket
in the stage2 shellcode.
