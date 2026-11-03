"""Which of a person's clients a question names, in any Russian case form.

"Тамбовского Бекона" must find "Тамбовский Бекон", while "бутик" alone must not
find "Арома-Бутик": every meaningful word of a name or alias has to appear, each
compared by its stem. A login is matched as a whole word."""

import re

# Words that say nothing about which client is meant.
GENERIC = frozenset(
    {"ооо", "оао", "зао", "group", "групп", "группа", "компания", "клиент", "бренд", "the"}
)
WORD = re.compile(r"[a-zа-я0-9]+")


def words(text):
    return WORD.findall(text.casefold().replace("ё", "е"))


def stem(word):
    """Cut a case ending: тамбовский → тамбовс, бекон stays (бекона, беконом)."""
    if len(word) >= 8:
        return word[:-3]
    if len(word) >= 6:
        return word[:-2]
    return word


LATIN = [
    ("sch", "щ"), ("ph", "ф"), ("sh", "ш"), ("ch", "ч"), ("kh", "х"), ("zh", "ж"),
    ("ya", "я"), ("yu", "ю"), ("ts", "ц"), ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"),
    ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"), ("j", "дж"), ("k", "к"),
    ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("q", "к"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"), ("y", "й"),
    ("z", "з"),
]  # fmt: skip


def cyrillic(word):
    """Phosagro → фосагро, Grand → гранд: managers write Latin names in Russian."""
    for latin, russian in LATIN:
        word = word.replace(latin, russian)
    return word


def found(word, tokens):
    forms = {stem(word), stem(cyrillic(word))}
    return any(token.startswith(form) for token in tokens for form in forms)


def named_by(reference, tokens):
    meaningful = [w for w in words(reference) if len(w) >= 4 and w not in GENERIC]
    if not meaningful:
        # Short names ("Дом", "ABC") only count as the whole phrase.
        phrase = " ".join(words(reference))
        return bool(phrase) and f" {phrase} " in f" {' '.join(tokens)} "
    # All words, or a distinctive first word alone: "Гранд" names Grand Line, while
    # "бутик" alone does not name Арома-Бутик.
    return all(found(w, tokens) for w in meaningful) or (
        len(meaningful[0]) >= 5 and found(meaningful[0], tokens)
    )


def mentioned(clients, text):
    """Ids of the clients the text names, in the order of ``clients``."""
    tokens = words(text)
    folded = text.casefold()
    found = []
    for client in clients:
        login = client.direct.client_login.casefold()
        if re.search(r"(?<![\w.-])" + re.escape(login) + r"(?![\w-])", folded) or any(
            named_by(reference, tokens) for reference in [client.name, *client.aliases]
        ):
            found.append(client.id)
    return found
