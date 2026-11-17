#!/usr/bin/env python3
import socket, sys, threading, os

HOST = sys.argv[1] if len(sys.argv) > 1 else '0.0.0.0'
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9000
COUNT = sys.argv[3] if len(sys.argv) > 3 else None

lock = threading.Lock()

def handle(c):
    if COUNT:
        with lock:
            with open(COUNT, 'a') as f:
                f.write('1\n')
    with c:
        while True:
            d = c.recv(4096)
            if not d:
                break
            c.sendall(d)

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((HOST, PORT))
s.listen()
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
