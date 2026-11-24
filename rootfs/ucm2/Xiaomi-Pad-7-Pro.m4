# SPDX-License-Identifier: BSD-3-Clause
# AudioReach topology for Xiaomi Pad 7 Pro (muyu, SM8635)
# - MultiMedia1 -> QUINARY_MI2S_RX (Quad AW88261 SmartPAs)
# - MultiMedia2 -> DISPLAY_PORT_RX_0 (MDSS DP0)
# - MultiMedia3 <- TX_CODEC_DMA_TX_3 (WCD9395 Built-in AMIC1/3/4/5)
include(`audioreach/audioreach.m4')
include(`audioreach/stream-subgraph.m4')
include(`audioreach/device-subgraph.m4')
include(`util/route.m4')
include(`util/mixer.m4')
include(`audioreach/tokens.m4')

dnl Playback MultiMedia1 - Speakers (QUINARY_MI2S_RX)
STREAM_SG_PCM_ADD(audioreach/subgraph-stream-vol-playback.m4, FRONTEND_DAI_MULTIMEDIA1,
	`S16_LE', 48000, 48000, 2, 2,
	0x00004001, 0x00004001, 0x00006001, `110000')

dnl Playback MultiMedia2 - DisplayPort 0
STREAM_SG_PCM_ADD(audioreach/subgraph-stream-vol-playback.m4, FRONTEND_DAI_MULTIMEDIA2,
	`S16_LE', 48000, 48000, 2, 2,
	0x00004002, 0x00004002, 0x00006010, `110000')

dnl Capture MultiMedia3 - WCD9395 Microphones (TX_CODEC_DMA_TX_3)
STREAM_SG_PCM_ADD(audioreach/subgraph-stream-capture.m4, FRONTEND_DAI_MULTIMEDIA3,
	`S16_LE', 48000, 48000, 1, 2,
	0x00004003, 0x00004003, 0x00006020, `110000')

dnl Quinary MI2S Playback (LPI I2S1 GPIO 6-9 via VA LPAIF -> 4x AW88261)
DEVICE_SG_ADD(audioreach/subgraph-device-i2s-playback.m4, `Quinary', QUINARY_MI2S_RX,
	`S16_LE', 48000, 48000, 2, 2,
	LPAIF_INTF_TYPE_VA, I2S_INTF_TYPE_PRIMARY, SD_LINE_IDX_I2S_SD1, DATA_FORMAT_FIXED_POINT,
	0x00004006, 0x00004006, 0x00006060, `QUINARY_MI2S_RX')

dnl DisplayPort 0 Playback
DEVICE_SG_ADD(audioreach/subgraph-device-display-port-playback.m4, `DISPLAY_PORT_RX_0', DISPLAY_PORT_RX_0,
	`S16_LE', 48000, 48000, 2, 2,
	0, 0, 0, DATA_FORMAT_FIXED_POINT,
	0x00004010, 0x00004010, 0x000060B0, `DISPLAY_PORT_RX_0')

dnl WCD9395 TX Capture (TX_CODEC_DMA_TX_3)
DEVICE_SG_ADD(audioreach/subgraph-device-codec-dma-capture.m4, `TX_CODEC_DMA_TX_3', TX_CODEC_DMA_TX_3,
	`S16_LE', 48000, 48000, 1, 2,
	LPAIF_INTF_TYPE_RXTX, CODEC_INTF_IDX_TX3, 0, DATA_FORMAT_FIXED_POINT,
	0x0000400B, 0x0000400B, 0x000060A0)

STREAM_DEVICE_PLAYBACK_MIXER(QUINARY_MI2S_RX, ``QUINARY_MI2S_RX'', ``MultiMedia1'')
STREAM_DEVICE_PLAYBACK_MIXER(DISPLAY_PORT_RX_0, ``DISPLAY_PORT_RX_0'', ``MultiMedia2'')

STREAM_DEVICE_PLAYBACK_ROUTE(QUINARY_MI2S_RX, ``QUINARY_MI2S_RX Audio Mixer'', ``MultiMedia1, stream0.logger1'')
STREAM_DEVICE_PLAYBACK_ROUTE(DISPLAY_PORT_RX_0, ``DISPLAY_PORT_RX_0 Audio Mixer'', ``MultiMedia2, stream1.logger1'')

STREAM_DEVICE_CAPTURE_MIXER(FRONTEND_DAI_MULTIMEDIA3, ``TX_CODEC_DMA_TX_3'')
STREAM_DEVICE_CAPTURE_ROUTE(FRONTEND_DAI_MULTIMEDIA3, ``MultiMedia3 Mixer'', ``TX_CODEC_DMA_TX_3, device120.logger1'')
