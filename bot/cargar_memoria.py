"""Carga en la memoria común los documentos de partida de memoria_inicial/.

Se ejecuta una vez, después de `ant apply`. Si un documento ya existe en la memoria,
no lo toca: los agentes pueden haberlo actualizado.
"""

import json
from pathlib import Path

import anthropic
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")


def main() -> None:
    lock = json.loads((RAIZ / "claude-lock.json").read_text())
    memoria = next(r["id"] for ruta, r in lock["resources"].items() if ruta.endswith("memory_store-aelia.yaml"))
    cliente = anthropic.Anthropic()
    origen = RAIZ / "memoria_inicial"
    for archivo in sorted(origen.rglob("*.md")):
        ruta = "/" + archivo.relative_to(origen).as_posix()
        try:
            cliente.beta.memory_stores.memories.create(memoria, path=ruta, content=archivo.read_text())
            print(f"Cargado {ruta}")
        except anthropic.ConflictError:
            print(f"Ya existía {ruta}, no lo toco")


if __name__ == "__main__":
    main()
