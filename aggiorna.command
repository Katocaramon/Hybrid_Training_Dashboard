#!/bin/bash
# Doppio click da Finder: scarica le sedute nuove da Hevy (e, se c'e' la
# chiave, corse e recupero da Garmin), rigenera la dashboard e la apre. Serve
# per non dover aprire il Terminale a mano.
#
# Le chiavi API si cercano, in ordine:
#   1. nelle variabili d'ambiente HEVY_API_KEY e GARMIN_PERSONAL_API_KEY
#   2. in ~/.strength-tracker.env  (una riga per chiave:  NOME=valore)
# Il secondo serve perche' un programma lanciato dal Finder non legge il tuo
# ~/.zshrc: senza, la chiave esportata li' resterebbe invisibile.

cd "$(dirname "$0")" || exit 1

pausa() { echo; read -n 1 -s -r -p "Premi un tasto per chiudere."; echo; }

if { [ -z "$HEVY_API_KEY" ] || [ -z "$GARMIN_PERSONAL_API_KEY" ]; } \
   && [ -f "$HOME/.strength-tracker.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$HOME/.strength-tracker.env"
    set +a
fi

if ! command -v uv >/dev/null 2>&1; then
    for prefisso in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
        [ -x "$prefisso/uv" ] && PATH="$prefisso:$PATH" && break
    done
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "Non trovo 'uv'. Installalo con:  brew install uv"
    pausa; exit 1
fi

echo "Scarico le sedute nuove da Hevy..."
if ! uv run strength-tracker sync-hevy; then
    echo
    echo "Sincronizzazione fallita: leggi il messaggio qui sopra."
    echo "Se manca la chiave API, crea ~/.strength-tracker.env con dentro:"
    echo "    HEVY_API_KEY=la-tua-chiave"
    pausa; exit 1
fi

# La corsa e' facoltativa: senza chiave si salta, senza fermare il resto.
if [ -n "$GARMIN_PERSONAL_API_KEY" ]; then
    echo
    echo "Scarico corse e recupero da Garmin..."
    if ! uv run strength-tracker sync-garmin; then
        echo "Garmin non ha risposto: vado avanti con i dati di palestra."
    fi
fi

echo
echo "Rigenero la dashboard..."
if ! uv run strength-tracker report; then
    pausa; exit 1
fi

open output/dashboard.html
echo
echo "Fatto. La dashboard e' aperta nel browser."
sleep 2
