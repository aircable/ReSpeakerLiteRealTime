Act as an expert Embedded Systems Firmware Engineer specializing in low-latency audio transmission over FreeRTOS. Build a complete, highly optimized C++ firmware application using the Arduino framework for a Seeed Studio ReSpeaker Lite Kit featuring an onboard XIAO ESP32-S3.

Target Goals:
1. Initialize a persistent full-duplex WebSocket client connection to a backend server. 
2. Stream continuous raw 16-bit PCM bidirectional audio over an I2S bus configured for full-duplex master transceiver mode.
3. Match the 48kHz audio profile expected by the ReSpeaker Lite's pre-flashed XMOS XU316 coprocessor.
4. Implement asymmetrical double-buffering using lockless ring buffers to isolate raw I2S DMA interrupts from the network network stack execution loop, preventing audio crackle or packet loss.

Hardware Pin Definitions:
- I2S_MCLK = GPIO 7
- I2S_BCK = GPIO 8
- I2S_WS = GPIO 9
- I2S_DIN = GPIO 10
- I2S_DOUT = GPIO 4

Software Constraints:
- Audio Config: Sample Rate 48000Hz, Stereo/Dual Channel, 16-bit depth.
- WebSockets: Send raw binary frames (`sendBIN`) continuously as soon as the input buffer exceeds 512 bytes. Stream incoming server frames immediately back out to the I2S Write DMA.
- Multi-threading: Place the I2S capture/playback loops on Core 0 via xTaskCreatePinnedToCore, leaving Core 1 entirely free to process WiFi and WebSocket payloads.

Provide only production-grade, highly readable code with verbose logging flags, robust auto-reconnect loops for WiFi dropped connections, and clear error handshakes. Do not include boilerplate text or generic summaries.

A detailled plan is in the file plan.md.
