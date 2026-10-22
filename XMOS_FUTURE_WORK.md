# Potential future work: XMOS voice processing

These are research candidates, **not release requirements**. The current ReSpeaker Lite
FFVA firmware and gateway should remain unchanged while we gain real-world experience
with playback, barge-in, wake words, and background noise. Revisit an item only when
observations show a specific problem it might solve, and compare it against the
current working firmware with the same recordings and listening conditions.

The present FFVA pipeline already applies AEC, two-microphone interference
cancellation with VNR, stationary noise suppression, and AGC. These components are
not waiting to be enabled; the opportunities below concern how we use and tune them.

## Candidates

1. **Expose XMOS speech and echo metadata to the gateway.** The XMOS VNR stage
   estimates speech relative to noise and already informs internal processing. A
   future control message could carry VNR, reference-active state, and selected AEC
   quality/activity values alongside the audio stream. Compare a combined
   VNR-plus-level barge-in gate against today's RMS threshold, especially for
   keyboard clicks, distant voices, and residual assistant playback. VNR is not
   an absolute loudness measure, so it should not replace the minimum RMS gate.
   Keep the audio path and existing thresholds unchanged until recordings support
   a better decision rule.

2. **Evaluate a fixed two-microphone pickup pattern.** A switchable broadside
   beamformer could favor a user on the perpendicular bisector of the two mics.
   Compare it with the existing adaptive interference canceller using on-axis and
   off-axis speech. Simple summation will give limited, frequency-dependent
   rejection and cannot distinguish front from back; it is not a replacement for
   AEC of our own speaker. Preserve the present FFVA path as the control case.

3. **Add a diagnostic USB-audio image.** XMOS documents a combination of I2S
   reference input and USB processed-audio output. An alternate development image
   could expose raw and processed channels to a computer for synchronized A/B
   recordings, without depending on the ESP32 or gateway diagnostic upload path.
   This should be separate from the production I2S/DFU image until its routing,
   timing, and recovery behavior have been verified on ReSpeaker Lite.

4. **Consider XMOS-local wake words or a few offline commands.** The far-field
   local-command, low-power command, and ASR-porting examples show possible
   architectures. This could eventually move some detection work off the ESP32,
   but our MicroWakeWord models are not drop-in XMOS models. Model conversion or
   a new inference port, resource measurements, and licensing review would be
   needed. The bundled Sensory and Cyberon recognizers are evaluation/commercially
   licensed components, not a ready-to-ship substitute.

## Lower priority for this board

- The ASRC example bridges independent audio clock domains. Our current XMOS-master
  I2S path was chosen specifically to avoid that problem; revisit ASRC only if the
  clock topology changes.
- The 16-microphone aggregator targets different microphone hardware, not the
  ReSpeaker Lite's two-mic array.
- Low-power tile switching becomes relevant if battery operation or a defined
  sleep-power target becomes part of the product requirements.

## Source material

- [XMOS FFVA example and pipeline](https://www.xmos.com/documentation/XM-014785-PC/html/doc/quick_start_guide/02.3_ffva.html)
- [XMOS interference canceller](https://www.xmos.com/documentation/XM-014785-PC/html/modules/voice/doc/rst/audio_processing/ic.html)
- [XMOS voice-to-noise estimator](https://www.xmos.com/documentation/XM-014785-PC/html/modules/voice/doc/rst/audio_processing/vnr.html)
- [XMOS I2S/USB configuration how-to](https://www.xmos.com/documentation/XM-014785-PC/html/doc/programming_guide/05_howto.html)
- [XMOS far-field local-command example](https://www.xmos.com/documentation/XM-014785-PC/html/doc/quick_start_guide/02.1_ffd.html)
- [XMOS ASR porting guide](https://www.xmos.com/documentation/XM-014785-PC/html/doc/programming_guide/asr/modifying.html)
