Synthetic speech for the voice tests, made with espeak-ng (16 kHz, mono, 16-bit):

- `question.wav`: "What is on my calendar today?"
- `confirm.wav`: "Confirm."

Regenerate with `espeak-ng -v en-gb -s 150 -w out.wav "<text>"`, then resample to 16 kHz.
