"""主界面实时信息取数：天气与农历。

天气优先使用 Windows 设备定位，失败时回退 IP 近似定位；农历直接调用
Windows 自带的中国农历日历，两者都不引入第三方依赖。
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LOCATION_URL = "https://ipwho.is/"
WEATHER_REFRESH_SECONDS = 600
WEATHER_RETRY_SECONDS = 60
WEATHER_MAX_AGE_SECONDS = 3600
NETWORK_TIMEOUT_SECONDS = 10
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
WEATHER_CODES = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪", 80: "小阵雨", 81: "阵雨",
    82: "强阵雨", 85: "阵雪", 86: "强阵雪", 95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}


@dataclass(frozen=True)
class Weather:
    location: str
    source: str
    temperature: float
    description: str
    observed_at: datetime

    def display(self, now: datetime) -> str:
        age = (now.astimezone(timezone.utc) - self.observed_at).total_seconds()
        if age > WEATHER_MAX_AGE_SECONDS or age < -300:
            return "天气数据已过期，等待更新"
        return f"{self.temperature:g}℃  {self.description}"


def _powershell_json(script: str, timeout: int = 12) -> dict:
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
         "$ErrorActionPreference = 'Stop'; " + script],
        capture_output=True, encoding="utf-8", errors="strict", timeout=timeout,
        check=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    data = json.loads(result.stdout.lstrip("\ufeff"))
    if not isinstance(data, dict):
        raise ValueError("系统返回的数据格式无效")
    return data


def _read_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "ShiChenFa/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read(1_000_001).decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("服务返回的数据格式无效")
    return data


def _coordinate(value: object, limit: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("定位未返回有效坐标")
    value = float(value)
    if not math.isfinite(value) or abs(value) > limit:
        raise ValueError("定位坐标超出范围")
    return value


def fetch_weather() -> Weather:
    # Windows 位置服务无数据时使用 IP 近似定位，界面明确区分两种来源。
    try:
        position = _powershell_json("""
Add-Type -AssemblyName System.Device;
$watcher = [System.Device.Location.GeoCoordinateWatcher]::new();
try {
    $null = $watcher.TryStart($false, [TimeSpan]::FromSeconds(5));
    $point = $watcher.Position.Location;
    if ($point.IsUnknown) { throw '设备定位不可用' };
    @{latitude=$point.Latitude; longitude=$point.Longitude} | ConvertTo-Json -Compress;
} finally { $watcher.Dispose() }
""")
        latitude = _coordinate(position.get("latitude"), 90)
        longitude = _coordinate(position.get("longitude"), 180)
        location, source = "当前位置", "设备定位"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logging.getLogger(__name__).info("设备定位不可用，改用 IP 近似定位：%s", exc)
        position = _read_json(LOCATION_URL)
        if position.get("success") is not True:
            raise ValueError("自动定位失败，请检查网络或位置权限")
        latitude = _coordinate(position.get("latitude"), 90)
        longitude = _coordinate(position.get("longitude"), 180)
        location = str(position.get("city") or position.get("region") or "当前位置")
        source = "IP 近似定位"
    query = urlencode({
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,weather_code,is_day", "temperature_unit": "celsius",
        "timezone": "auto", "timeformat": "unixtime",
    })
    payload = _read_json(f"{WEATHER_URL}?{query}")
    current = payload.get("current")
    if not isinstance(current, dict):
        raise ValueError("天气服务未返回当前天气")
    temperature = current.get("temperature_2m")
    stamp = current.get("time")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (temperature, stamp)):
        raise ValueError("天气服务返回了无效的温度或时间")
    code = current.get("weather_code")
    if isinstance(code, bool) or not isinstance(code, int) or code not in WEATHER_CODES:
        raise ValueError("天气服务返回了未知天气类型")
    if payload.get("current_units", {}).get("temperature_2m") != "°C":
        raise ValueError("天气服务返回了不支持的温度单位")
    observed = datetime.fromtimestamp(stamp, timezone.utc)
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not -300 <= age <= WEATHER_MAX_AGE_SECONDS:
        raise ValueError("天气服务返回的数据已过期")
    return Weather(location, source, float(temperature), WEATHER_CODES[code], observed)


def lunar_date(day: date) -> tuple[str, str]:
    # 使用 Windows 自带的中国农历，闰月在月份序号中单独占一月。
    data = _powershell_json("""
$calendar = [System.Globalization.ChineseLunisolarCalendar]::new();
$day = [datetime]::ParseExact('%s', 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture);
$year = $calendar.GetYear($day);
@{month=$calendar.GetMonth($day); day=$calendar.GetDayOfMonth($day);
  leap=$calendar.GetLeapMonth($year); cycle=$calendar.GetSexagenaryYear($day)} | ConvertTo-Json -Compress;
""" % day.isoformat())
    month, lunar_day, leap, cycle = (int(data[key]) for key in ("month", "day", "leap", "cycle"))
    if not (1 <= month <= 13 and 1 <= lunar_day <= 30 and 0 <= leap <= 13 and 1 <= cycle <= 60):
        raise ValueError("系统农历数据无效")
    is_leap = leap > 0 and month == leap
    if leap and month >= leap:
        month -= 1
    months = ("正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊")
    digits = "一二三四五六七八九十"
    if lunar_day <= 10:
        day_name = "初" + digits[lunar_day - 1]
    elif lunar_day < 20:
        day_name = "十" + digits[lunar_day - 11]
    elif lunar_day == 20:
        day_name = "二十"
    elif lunar_day < 30:
        day_name = "廿" + digits[lunar_day - 21]
    else:
        day_name = "三十"
    stem = "甲乙丙丁戊己庚辛壬癸"[(cycle - 1) % 10]
    branch = "子丑寅卯辰巳午未申酉戌亥"[(cycle - 1) % 12]
    animal = "鼠牛虎兔龙蛇马羊猴鸡狗猪"[(cycle - 1) % 12]
    return f"{'闰' if is_leap else ''}{months[month - 1]}月{day_name}", f"{stem}{branch}年  {animal}年"
