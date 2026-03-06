import httpx
import asyncio
import json

async def main():
    base = "https://saavn.sumit.co/api"
    url = f"{base}/songs?ids=i-4OQoee"
    with open("test_api_log.txt", "w", encoding="utf-8") as f:
        f.write(f"--- {base} ---\n")
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True, verify=False) as client:
                response = await client.get(url)
                f.write(f"Status: {response.status_code}\n")
                if response.status_code == 200:
                    data = response.json()
                    
                    if isinstance(data, dict):
                        d = data.get("data")
                        if isinstance(d, list) and len(d) > 0:
                            f.write(f"Details Sample Title: {d[0].get('name')}\n")
                        elif isinstance(d, dict) and len(d) > 0:
                            f.write(f"Details Sample Title: {list(d.values())[0].get('name')}\n")
                        else:
                            f.write(f"Unexpected Dict: {json.dumps(data)[:200]}\n")
                    elif isinstance(data, list) and len(data) > 0:
                        f.write(f"Details Sample Title: {data[0].get('name')}\n")
                else:
                    f.write(f"Text: {response.text[:100]}\n")
        except Exception as e:
            f.write(f"ERROR: {type(e).__name__}\n")

asyncio.run(main())
