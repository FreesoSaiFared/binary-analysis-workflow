#!/usr/bin/env python3
import argparse,array,os,selectors,socket,struct
def send(s,v,fd=None):
    p=struct.pack('<q',v)
    if fd is None:s.sendall(p)
    else:s.sendmsg([p],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
p=argparse.ArgumentParser();p.add_argument('-S',required=True);p.add_argument('-m',required=True);p.add_argument('-l',type=int,default=16777216);p.add_argument('--peers',type=int,default=4);a=p.parse_args()
for x in(a.S,a.m):
    try:os.unlink(x)
    except FileNotFoundError:pass
shm=os.open(a.m,os.O_CREAT|os.O_RDWR,0o600);os.ftruncate(shm,a.l);ls=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);ls.bind(a.S);ls.listen(a.peers);peers=[]
print(f'IVSHMEM_SERVER_READY socket={a.S} size={a.l}',flush=True)
for pid in range(a.peers):
    c,_=ls.accept();ev=os.eventfd(0,os.EFD_CLOEXEC|os.EFD_NONBLOCK);send(c,0);send(c,pid);send(c,-1,shm)
    for oid,oc,oe in peers:send(c,oid,oe)
    for oid,oc,oe in peers:send(oc,pid,ev)
    send(c,pid,ev);peers.append((pid,c,ev));print(f'IVSHMEM_PEER_CONNECTED id={pid}',flush=True)
print(f'IVSHMEM_ALL_PEERS count={len(peers)}',flush=True);sel=selectors.DefaultSelector()
for pid,c,_ in peers:c.setblocking(False);sel.register(c,selectors.EVENT_READ,pid)
while peers:
    for key,_ in sel.select(1):
        try:d=key.fileobj.recv(1)
        except BlockingIOError:continue
        if d==b'':
            print(f'IVSHMEM_PEER_DISCONNECTED id={key.data}',flush=True)
            try:sel.unregister(key.fileobj);key.fileobj.close()
            except Exception:pass
            peers[:]=[x for x in peers if x[0]!=key.data]
