"""Status from OurAirports type; IANA timezone from geographic boundaries."""
from functools import lru_cache


@lru_cache(maxsize=1)
def finder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder()


@lru_cache(maxsize=100000)
def timezone(latitude, longitude):
    if latitude is None or longitude is None:
        return None
    try:
        lat, lng = float(latitude), float(longitude)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return None
        return finder().timezone_at(lat=lat, lng=lng)
    except (ValueError, TypeError):
        return None


def enrich(record, source_type=None):
    facility_type = source_type or record.get('facility_type')
    if facility_type:
        record['status'] = 'close' if str(facility_type).casefold() in {'closed', 'closed airport'} else 'open'
        record['status_source'] = 'OurAirports facility type (closed versus active facility types)'
    else:
        record['status'] = None
        record['status_source'] = None
    record['timezone'] = timezone(record.get('latitude_deg'), record.get('longitude_deg'))
    record['timezone_source'] = 'timezonefinder / IANA polygon lookup' if record['timezone'] else None
    return record
