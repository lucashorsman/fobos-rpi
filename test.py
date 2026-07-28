import asyncio, json, websockets

async def send(cmd: dict):
    async with websockets.connect("ws://fobos-pi.ucolick.org:8765/control") as ws:
        await ws.send(json.dumps(cmd))
        print(await ws.recv())

asyncio.run(send({"cmd": "laser_off", "intensity": 1.0}))