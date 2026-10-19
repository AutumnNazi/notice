"""主界面实时信息取数：天气与农历。

天气优先使用 Windows 设备定位，失败时回退 IP 近似定位；农历直接调用
Windows 自带的中国农历日历，两者都不引入第三方依赖。
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from core.app_logging import get_logger


_LOGGER = get_logger(__name__)

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
LOCATION_URL = "https://ipwho.is/"
# 定位服务只给拼音或英文城市名，用与天气同源的地理编码接口换中文名。
CITY_SEARCH_URL = "https://geocoding-api.open-meteo.com/v1/search"
# 设备定位只给坐标，靠免费逆地理服务补地名；该接口国内可达性差，只作最后手段。
REVERSE_GEOCODE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
# 搜索结果与定位坐标的同城容差，避免代理或 VPN 下把别处地名安到当前位置上。
CITY_MATCH_DEGREES = 0.5
WEATHER_REFRESH_SECONDS = 600
WEATHER_RETRY_SECONDS = 60
LUNAR_RETRY_SECONDS = 60
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
    try:
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
             "$ErrorActionPreference = 'Stop'; " + script],
            capture_output=True, encoding="utf-8", errors="strict", timeout=timeout,
            check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 记录超时与异常类型：用于区分 PowerShell 缺失、被策略拦截还是脚本卡死
        _LOGGER.warning("调用系统 PowerShell 失败（超时 %ss）：%s: %s",
                        timeout, type(exc).__name__, exc, exc_info=True)
        raise
    try:
        data = json.loads(result.stdout.lstrip("\ufeff"))
    except ValueError as exc:
        # 保留输出片段，才能看出是编码问题还是脚本自身报错
        _LOGGER.warning("PowerShell 返回内容无法解析：%s: %s；原始输出=%r",
                        type(exc).__name__, exc, result.stdout[:200])
        raise
    if not isinstance(data, dict):
        _LOGGER.warning("PowerShell 返回了非对象 JSON：%r", data)
        raise ValueError("系统返回的数据格式无效")
    return data


def _read_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "ShiChenFa/1.0", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=NETWORK_TIMEOUT_SECONDS) as response:
            body = response.read(1_000_001)
    except OSError as exc:
        # HTTPError 是 URLError 子类，状态码在 code 上；带上 URL 才能区分是定位还是天气失败
        _LOGGER.warning("网络请求失败：url=%s 状态码=%s %s: %s",
                        url, getattr(exc, "code", "-"), type(exc).__name__, exc, exc_info=True)
        raise
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except ValueError as exc:
        _LOGGER.warning("服务返回内容无法解析：url=%s %s: %s；原始内容=%r",
                        url, type(exc).__name__, exc, body[:200])
        raise
    if not isinstance(data, dict):
        _LOGGER.warning("服务返回了非对象 JSON：url=%s 类型=%s", url, type(data).__name__)
        raise ValueError("服务返回的数据格式无效")
    return data


def _coordinate(value: object, limit: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("定位未返回有效坐标")
    value = float(value)
    if not math.isfinite(value) or abs(value) > limit:
        raise ValueError("定位坐标超出范围")
    return value


def _reverse_geocode(latitude: float, longitude: float) -> str:
    # 设备定位只返回坐标，反查城市名才能让位置名与天气出自同一坐标；查不到交给调用方兜底。
    query = urlencode({"latitude": latitude, "longitude": longitude, "localityLanguage": "zh"})
    try:
        data = _read_json(f"{REVERSE_GEOCODE_URL}?{query}")
    except (OSError, ValueError) as exc:
        _LOGGER.warning("逆地理查询失败，位置名改用兜底文案：url=%s %s: %s",
                        REVERSE_GEOCODE_URL, type(exc).__name__, exc)
        return ""
    city = str(data.get("city") or data.get("locality") or "").strip()
    if not city:
        # 定位准却显示不出地名，正是静默兜底造成的，这里必须留痕
        _LOGGER.warning("逆地理未返回城市名，位置名改用兜底文案：url=%s 顶层字段=%s",
                        REVERSE_GEOCODE_URL, sorted(data.keys()))
        return ""
    return city


def _search_city_in_chinese(name: str, latitude: float, longitude: float) -> str:
    # 定位服务给的是拼音或英文，借与天气同源的地理编码接口换成中文名
    query = urlencode({"name": name, "count": 1, "language": "zh", "format": "json"})
    try:
        data = _read_json(f"{CITY_SEARCH_URL}?{query}")
    except (OSError, ValueError) as exc:
        _LOGGER.warning("城市名搜索失败，保留定位服务原名：url=%s %s: %s",
                        CITY_SEARCH_URL, type(exc).__name__, exc)
        return ""
    results = data.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        _LOGGER.warning("城市名搜索没有可用结果，保留定位服务原名：url=%s 名称=%s",
                        CITY_SEARCH_URL, name)
        return ""
    found = results[0]
    try:
        found_latitude = _coordinate(found.get("latitude"), 90)
        found_longitude = _coordinate(found.get("longitude"), 180)
    except ValueError as exc:
        _LOGGER.warning("城市名搜索返回了无效坐标，保留定位服务原名：url=%s %s: %s",
                        CITY_SEARCH_URL, type(exc).__name__, exc)
        return ""
    if abs(found_latitude - latitude) > CITY_MATCH_DEGREES or abs(found_longitude - longitude) > CITY_MATCH_DEGREES:
        # 代理或 VPN 会让 IP 城市名落到别处，这种名字比原名更容易误导用户
        _LOGGER.warning("城市名搜索结果与定位坐标不同城，保留定位服务原名：名称=%s 搜索=(%.2f, %.2f) 定位=(%.2f, %.2f)",
                        name, found_latitude, found_longitude, latitude, longitude)
        return ""
    return str(found.get("name") or "").strip()


def _ip_service_city() -> str:
    # 设备定位不返回地名，借 IP 定位的城市名当取名线索
    try:
        position = _read_json(LOCATION_URL)
    except (OSError, ValueError) as exc:
        _LOGGER.warning("IP 定位补名失败，改用坐标反查地名：url=%s %s: %s",
                        LOCATION_URL, type(exc).__name__, exc)
        return ""
    if position.get("success") is not True:
        _LOGGER.warning("IP 定位补名未返回成功结果，改用坐标反查地名：url=%s 提示=%s",
                        LOCATION_URL, position.get("message") or "-")
        return ""
    return str(position.get("city") or position.get("region") or "").strip()


def _localized_city(latitude: float, longitude: float, service_city: str = "") -> str:
    # 位置名统一成中文：先换名，换不到再按坐标反查，最后才落到兜底文案
    if service_city:
        chinese = _search_city_in_chinese(service_city, latitude, longitude)
        if chinese:
            return chinese
    return _reverse_geocode(latitude, longitude) or service_city or "当前位置"


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
        service_city = _ip_service_city()
        location, source = _localized_city(latitude, longitude, service_city), "设备定位"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        # 降级路径：记录异常类型与原因，便于判断是位置权限、服务缺失还是系统报错
        _LOGGER.warning("设备定位不可用，改用 IP 近似定位：%s: %s", type(exc).__name__, exc)
        position = _read_json(LOCATION_URL)
        if position.get("success") is not True:
            _LOGGER.warning("IP 定位服务未返回成功结果：url=%s 提示=%s",
                            LOCATION_URL, position.get("message") or "-")
            raise ValueError("自动定位失败，请检查网络或位置权限")
        latitude = _coordinate(position.get("latitude"), 90)
        longitude = _coordinate(position.get("longitude"), 180)
        service_city = str(position.get("city") or position.get("region") or "")
        location, source = _localized_city(latitude, longitude, service_city), "IP 近似定位"
    query = urlencode({
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,weather_code,is_day", "temperature_unit": "celsius",
        "timezone": "auto", "timeformat": "unixtime",
    })
    payload = _read_json(f"{WEATHER_URL}?{query}")
    current = payload.get("current")
    if not isinstance(current, dict):
        _LOGGER.warning("天气服务未返回当前天气：url=%s 顶层字段=%s",
                        WEATHER_URL, sorted(payload.keys()))
        raise ValueError("天气服务未返回当前天气")
    temperature = current.get("temperature_2m")
    stamp = current.get("time")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) for value in (temperature, stamp)):
        _LOGGER.warning("天气服务返回了无效的温度或时间：温度=%r 时间=%r", temperature, stamp)
        raise ValueError("天气服务返回了无效的温度或时间")
    code = current.get("weather_code")
    if isinstance(code, bool) or not isinstance(code, int) or code not in WEATHER_CODES:
        _LOGGER.warning("天气服务返回了未知天气类型：code=%r", code)
        raise ValueError("天气服务返回了未知天气类型")
    units = payload.get("current_units")
    if not isinstance(units, dict) or units.get("temperature_2m") != "°C":
        _LOGGER.warning("天气服务返回了不支持的温度单位：current_units=%r",
                        payload.get("current_units"))
        raise ValueError("天气服务返回了不支持的温度单位")
    observed = datetime.fromtimestamp(stamp, timezone.utc)
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not -300 <= age <= WEATHER_MAX_AGE_SECONDS:
        _LOGGER.warning("天气服务返回的数据已过期：观测时间=%s 距今=%.0f 秒",
                        observed.astimezone().strftime("%Y-%m-%d %H:%M:%S"), age)
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
    values = [data.get(key) for key in ("month", "day", "leap", "cycle")]
    if any(type(value) is not int for value in values):
        _LOGGER.warning("系统农历数据无效：日期=%s 返回值=%s", day.isoformat(), data)
        raise ValueError("系统农历数据无效")
    month, lunar_day, leap, cycle = values
    if not (1 <= month <= (13 if leap else 12) and 1 <= lunar_day <= 30
            and (leap == 0 or 2 <= leap <= 13) and 1 <= cycle <= 60):
        _LOGGER.warning("系统农历数据无效：日期=%s 返回值=%s", day.isoformat(), data)
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
