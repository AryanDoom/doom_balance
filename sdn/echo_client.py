import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
count = int(sys.argv[3])
tag = sys.argv[4]

for index in range(count):
    message = f'{tag}-{index}'.encode()
    with socket.create_connection((host, port), 3) as client:
        client.sendall(message)
        response = b''
        while len(response) < len(message):
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
        print(response.decode(errors='replace'))
