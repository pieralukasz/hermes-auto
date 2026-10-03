# Szybki start

Instaluj rdzeń `hermes-auto` i tylko potrzebne adaptery. Instrukcja instalacji jest w głównym README.

**Todoist:** etykieta `hermes` przy zadaniu na dziś lub zaległym uruchamia pełnego agenta Hermes (narzędzia, skille, reguły). Agent robi to, co mówi tytuł i opis; rzeczy wymagające Twojej zgody (wysyłka, płatność, KSeF) przygotowuje i czeka. Cel i granice wpisz w opis. Zadanie z godziną czeka do tej godziny. Przesunięcie terminu nie tworzy nowej rozmowy; w rutynie nowe wystąpienie zaczyna się po ukończeniu poprzedniego. Działa to jak nowa sesja w aplikacji Hermes: pierwsza wiadomość to tytuł i opis zadania, agent ma Twoje skille, pamięć, reguły i podagentów, a odpowiedź ma normalne formatowanie.

**Gmail:** oznacz wybrany wątek etykietą `Hermes/Watch` (okrojony szkic) albo `Hermes/Agent` (pełny agent, tylko dla zaufanych nadawców).

**Proton:** oznacz wiadomość z wybranego wątku etykietą `Hermes Watch` albo `Hermes Agent`. Bridge musi działać.

Pierwsze sprawdzenie poczty zapamiętuje stan — nie uruchamia rozmów dla starych wiadomości. Późniejsza nowa odpowiedź od innej osoby tworzy sesję. Aby od razu rozpocząć śledzenie po dodaniu etykiety, uruchom `hermes-auto scan`. Usunięcie etykiety wyłącza śledzenie. Sprawdzenie co pięć minut nie powiela sesji.

Na Macu usługa sprawdza co minutę, czy aplikacja Hermes jest otwarta. Podczas jej działania odczytuje źródła co pięć minut. Nie ma stałej godziny 5:30. W konfiguracji możesz ustawić `desktop_only: false`, jeśli automat ma pracować również przy zamkniętej aplikacji.

`hermes-auto status` pokazuje przygotowane rozmowy i problemy. `pause` / `resume` zatrzymuje / wznawia automat. `retry ID` kontynuuje sprawdzoną przez Ciebie przerwaną pracę w tej samej sesji. `new-session ID` świadomie tworzy nową. Obie komendy dodają pracę do kolejki; wykonuje ją następne sprawdzenie lub `hermes-auto run`.

Nie usuwaj katalogu stanu, żeby „zresetować” automat: to rejestr zapobiegający duplikatom. Używaj jawnych poleceń i `hermes-auto backup`. Każdy pakiet można wyłączyć osobno: `hermes-auto source disable gmail`.

Maile z `Hermes Watch` są okrojone: szkic bez narzędzi albo (tryb `research`) tylko wyszukiwanie i odczyt stron. Agent nie wysyła wiadomości, nie zapisuje szkiców w skrzynce, nie płaci i nie zmienia zadań; przygotowuje to do Twojej akceptacji. Komendy wymagające zatwierdzenia są odrzucane. Treść wybranych zadań i nowych odpowiedzi jest przekazywana do modelu skonfigurowanego w Twoim Hermesie.
