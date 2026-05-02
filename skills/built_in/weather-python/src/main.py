import urllib.request
import urllib.error
def get_weather(city: str, format: str = "short") -> str:
    """Get the current weather for a city."""
    fmt = "?format=3" if format == "short" else ""
    url = f"https://wttr.in/{city}{fmt}"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8").strip()
    except urllib.error.URLError as exc:
        return f"Weather lookup failed for {city}: {exc}"
