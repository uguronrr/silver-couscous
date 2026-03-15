import random

class BrowserProfile:
    """Randomized but internally consistent browser profile per session."""

    _PROFILES = [
        {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
            "color_scheme": "light",
            "device_scale_factor": 2.0,
            "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
            "platform": "MacIntel",
            "hardware_concurrency": 8,
            "device_memory": 8,
        },
        {
            "viewport": {"width": 1536, "height": 864},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
            "color_scheme": "dark",
            "device_scale_factor": 1.25,
            "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "platform": "Win32",
            "hardware_concurrency": 12,
            "device_memory": 16,
        },
        {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "locale": "tr-TR",
            "timezone_id": "Europe/Istanbul",
            "color_scheme": "light",
            "device_scale_factor": 1.0,
            "sec_ch_ua": '"Chromium";v="135", "Google Chrome";v="135", "Not-A.Brand";v="99"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
            "platform": "Win32",
            "hardware_concurrency": 6,
            "device_memory": 8,
        },
    ]

    def __init__(self) -> None:
        profile = random.choice(self._PROFILES)
        self.viewport = profile["viewport"]
        self.user_agent = profile["user_agent"]
        self.locale = profile["locale"]
        self.timezone = profile["timezone_id"]
        self.color_scheme = profile["color_scheme"]
        self.device_scale_factor = profile["device_scale_factor"]
        self.sec_ch_ua = profile["sec_ch_ua"]
        self.sec_ch_ua_mobile = profile["sec_ch_ua_mobile"]
        self.sec_ch_ua_platform = profile["sec_ch_ua_platform"]
        self.platform = profile["platform"]
        self.hardware_concurrency = profile["hardware_concurrency"]
        self.device_memory = profile["device_memory"]

    def to_context_kwargs(self) -> dict:
        """Return Playwright new_context() kwargs."""
        return {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "color_scheme": self.color_scheme,
            "device_scale_factor": self.device_scale_factor,
        }

    def to_api_headers(self) -> dict:
        """Return headers for the curl_cffi session to match this profile."""
        lang = self.locale
        lang_short = lang.split("-")[0]
        return {
            "User-Agent": self.user_agent,
            "Accept-Language": f"{lang},{lang_short};q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": self.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self.sec_ch_ua_platform,
        }
