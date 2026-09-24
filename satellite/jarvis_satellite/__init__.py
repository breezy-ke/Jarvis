"""Jarvis satellite: say “Hey Jarvis” to your Windows PC and talk to Jarvis.

Nothing leaves the PC until the wake word is heard, and the wake word itself is
detected locally (openWakeWord). Then the microphone streams to your own Jarvis
over its voice socket, and Jarvis's replies play through your speakers.
"""

__version__ = "0.1.0"
