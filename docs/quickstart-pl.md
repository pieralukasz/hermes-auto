# Szybki start

Instaluj rdzeń `hermes-auto` i tylko potrzebne adaptery. Instrukcja instalacji jest w głównym README.

**Todoist:** etykieta `hermes` uruchamia automat dla zadania na dziś lub zaległego. Opcjonalnie dodaj jedną etykietę: `hermes-open` (sama rozmowa), `hermes-draft` (krótki szkic), `hermes-research` (research publicznych źródeł), `hermes-agent` (pełny agent Hermes z narzędziami i skillami; tylko Todoist). Bez dodatkowej etykiety powstaje szkic. Zadanie z godziną czeka do tej godziny; zadanie z samą datą jest gotowe przez cały dzień. Przesunięcie terminu nie tworzy nowej rozmowy. Dla rutyny kolejne ukończenie wyznacza nowe wystąpienie.

**Gmail:** oznacz wybrany wątek etykietą `Hermes/Watch`.

**Proton:** oznacz wiadomość z wybranego wątku etykietą `Hermes Watch`. Bridge musi działać.

Pierwsze sprawdzenie poczty zapamiętuje stan — nie uruchamia rozmów dla starych wiadomości. Późniejsza nowa odpowiedź od innej osoby tworzy sesję. Aby od razu rozpocząć śledzenie po dodaniu etykiety, uruchom `hermes-auto scan`. Usunięcie etykiety wyłącza śledzenie. Sprawdzenie co pięć minut nie powiela sesji.

Na Macu usługa sprawdza co minutę, czy aplikacja Hermes jest otwarta. Podczas jej działania odczytuje źródła co pięć minut. Nie ma stałej godziny 5:30. W konfiguracji możesz ustawić `desktop_only: false`, jeśli automat ma pracować również przy zamkniętej aplikacji.

`hermes-auto status` pokazuje przygotowane rozmowy i problemy. `pause` / `resume` zatrzymuje / wznawia automat. `retry ID` kontynuuje sprawdzoną przez Ciebie przerwaną pracę w tej samej sesji. `new-session ID` świadomie tworzy nową. Obie komendy dodają pracę do kolejki; wykonuje ją następne sprawdzenie lub `hermes-auto run`.

Nie usuwaj katalogu stanu, żeby „zresetować” automat: to rejestr zapobiegający duplikatom. Używaj jawnych poleceń i `hermes-auto backup`. Każdy pakiet można wyłączyć osobno: `hermes-auto source disable gmail`.

Szkic nie ma dostępu do narzędzi. Research może tylko wyszukiwać i odczytywać strony. Automat nie wysyła wiadomości, nie zapisuje szkiców w skrzynce, nie płaci i nie zmienia zadań. Treść wybranych zadań i nowych odpowiedzi jest przekazywana do modelu skonfigurowanego w Twoim Hermesie.
