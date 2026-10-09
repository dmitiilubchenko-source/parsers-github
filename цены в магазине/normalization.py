"""Convert confirmed retail prices to rub/kg or rub/l without guessing sizes."""
import re
from decimal import Decimal


def normalize(row):
    result = dict(normalized_price=None, normalized_unit='', package_quantity=None,
                  normalization_note='Обычная цена не подтверждена')
    price = row.get('regular_price')
    if price is None or price == '':
        return result
    price = Decimal(str(price))
    if not price.is_finite() or price <= 0:
        result['normalization_note'] = 'Некорректная цена'
        return result
    unit = re.sub(r'[\s/.]', '', str(row.get('price_unit', '')).casefold())
    if unit in {'кг', 'kg', 'л', 'l'}:
        return dict(normalized_price=float(price), normalized_unit='руб/кг' if unit in {'кг', 'kg'} else 'руб/л',
                    package_quantity=1.0, normalization_note='Источник уже указывает цену за кг/л; в корзине 1 кг/л')
    if unit not in {'шт', 'уп', 'упак', 'упаковка'}:
        result['normalization_note'] = 'Неизвестная продажная единица'
        return result
    text = row.get('name') or row.get('package') or ''
    # Ranges/approximate weights cannot give a reliable package conversion.
    if re.search(r'(?:~|≈|около|\bот\s+)\s*\d|\d+(?:[.,]\d+)?\s*[-–]\s*\d+\s*(?:кг|г|л|мл)\b', text, re.I):
        result['normalization_note'] = 'Приблизительная масса или диапазон'
        return result
    pattern = r'(?<![\d.,])(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>кг|мл|г|л)\b'
    matches = list(re.finditer(pattern, text, re.I))
    if len(matches) != 1:
        result['normalization_note'] = 'Нет однозначной массы/объёма упаковки'
        return result
    match = matches[0]
    amount = Decimal(match['amount'].replace(',', '.'))
    suffix = match['unit'].casefold()
    # Explicit multipacks: 6x0.5л, 6 × 500мл, 500мл x 6.
    before, after = text[:match.start()], text[match.end():]
    multiplier = re.search(r'(\d+)\s*[xх×*]\s*$', before, re.I)
    reverse = re.match(r'\s*[xх×*]\s*(\d+)\b', after, re.I)
    if multiplier and reverse:
        result['normalization_note'] = 'Неоднозначная мультиупаковка'
        return result
    if multiplier or reverse:
        amount *= int((multiplier or reverse).group(1))
    elif re.search(r'\d+\s*шт\b', text, re.I):
        result['normalization_note'] = 'Мультиупаковка без подтверждённой общей массы'
        return result
    if suffix in {'г', 'мл'}:
        amount /= 1000
    if amount <= 0:
        result['normalization_note'] = 'Некорректная масса/объём'
        return result
    return dict(normalized_price=float((price / amount).quantize(Decimal('0.01'))),
                normalized_unit='руб/кг' if suffix in {'г', 'кг'} else 'руб/л',
                package_quantity=float(amount), normalization_note='Пересчёт по указанной в названии массе/объёму')
