// kickoff_r3d <clip.R3D> <out.wav> [dylibFolder]
// Extracts all audio channels of a RED clip to 24-bit PCM WAV and prints clip metadata as one JSON line.
#include "R3DSDK.h"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>

using namespace R3DSDK;

#ifndef KICKOFF_DEFAULT_LIBS
#define KICKOFF_DEFAULT_LIBS "/Users/jakelundell/Downloads/R3DSDKv9_2_1/Redistributable/mac"
#endif

static int fail(const char *msg, int code = 1) { fprintf(stderr, "kickoff_r3d: %s\n", msg); return code; }

static void put16(FILE *f, uint16_t v) { uint8_t b[2] = {uint8_t(v), uint8_t(v >> 8)}; fwrite(b, 1, 2, f); }
static void put32(FILE *f, uint32_t v) { uint8_t b[4] = {uint8_t(v), uint8_t(v >> 8), uint8_t(v >> 16), uint8_t(v >> 24)}; fwrite(b, 1, 4, f); }

static void writeWavHeader(FILE *f, uint32_t channels, uint32_t rate, uint64_t dataBytes) {
    const uint32_t bytesPerSample = 3;
    uint32_t data32 = dataBytes > 0xFFFFFFFFull - 36 ? 0xFFFFFFFFu - 36 : uint32_t(dataBytes);
    fwrite("RIFF", 1, 4, f); put32(f, 36 + data32); fwrite("WAVE", 1, 4, f);
    fwrite("fmt ", 1, 4, f); put32(f, 16); put16(f, 1); put16(f, uint16_t(channels));
    put32(f, rate); put32(f, rate * channels * bytesPerSample);
    put16(f, uint16_t(channels * bytesPerSample)); put16(f, 24);
    fwrite("data", 1, 4, f); put32(f, data32);
}

static std::string jsonEscape(const std::string &s) {
    std::string o;
    for (char c : s) { if (c == '"' || c == '\\') o += '\\'; if ((unsigned char)c >= 0x20) o += c; }
    return o;
}

int main(int argc, char **argv) {
    if (argc < 3) return fail("usage: kickoff_r3d <clip.R3D> <out.wav> [dylibFolder]", 2);
    const char *clipPath = argv[1], *wavPath = argv[2];
    const char *libs = argc > 3 ? argv[3] : getenv("KICKOFF_R3D_LIBS");
    if (!libs || !*libs) libs = KICKOFF_DEFAULT_LIBS;

    InitializeStatus is = InitializeSdk(libs, OPTION_RED_NONE);
    if (is != ISInitializeOK) {
        FinalizeSdk();
        char m[1200]; snprintf(m, sizeof m, "InitializeSdk failed (status %d) with library folder %s", int(is), libs);
        return fail(m, 3);
    }

    int rc = 0;
    {
        Clip clip(clipPath);
        if (clip.Status() != LSClipLoaded) {
            char m[1200]; snprintf(m, sizeof m, "could not load clip (status %d): %s", int(clip.Status()), clipPath);
            rc = fail(m, 4);
        } else {
            const size_t channels = clip.AudioChannelCount();
            const unsigned long long totalSamples = clip.AudioSampleCount();
            unsigned int rate = clip.MetadataExists(RMD_SAMPLERATE) ? clip.MetadataItemAsInt(RMD_SAMPLERATE) : 48000;
            if (!rate) rate = 48000;
            const float fps = clip.VideoAudioFramerate();
            const size_t frames = clip.VideoFrameCount();
            const char *tc = clip.Timecode(0);
            if (!tc) tc = clip.AbsoluteTimecode(0);
            std::string timecode = tc ? tc : "";

            if (channels == 0 || totalSamples == 0) {
                rc = fail("clip has no audio", 5);
            } else {
                FILE *f = fopen(wavPath, "wb");
                if (!f) rc = fail("cannot open output WAV for writing", 6);
                else {
                    const uint64_t dataBytes = uint64_t(totalSamples) * channels * 3;
                    writeWavHeader(f, uint32_t(channels), rate, dataBytes);

                    // Decode in ~1 s chunks into a 512-byte aligned buffer (SDK requirement).
                    const size_t chunkSamples = rate;
                    size_t bufSize = chunkSamples * channels * 4;
                    bufSize = (bufSize + 511) & ~size_t(511);
                    void *buf = nullptr;
                    if (posix_memalign(&buf, 512, bufSize) != 0) buf = nullptr;
                    std::vector<uint8_t> out(chunkSamples * channels * 3);

                    unsigned long long pos = 0, written = 0;
                    while (buf && pos < totalSamples) {
                        size_t n = size_t(std::min<unsigned long long>(chunkSamples, totalSamples - pos));
                        if (clip.DecodeAudio(pos, &n, buf, bufSize) != DSDecodeOK || n == 0) break;
                        // Big-endian 32-bit words, 24-bit MSB aligned -> little-endian 24-bit.
                        const uint8_t *src = static_cast<const uint8_t *>(buf);
                        const size_t words = n * channels;
                        for (size_t i = 0; i < words; ++i) {
                            out[i * 3 + 0] = src[i * 4 + 2];
                            out[i * 3 + 1] = src[i * 4 + 1];
                            out[i * 3 + 2] = src[i * 4 + 0];
                        }
                        fwrite(out.data(), 1, words * 3, f);
                        pos += n; written += n;
                    }
                    free(buf);

                    if (!buf && written == 0 && pos == 0 && totalSamples) { /* allocation failed */ }
                    if (written != totalSamples) {
                        // Patch header to what was actually written, then report.
                        fseek(f, 0, SEEK_SET);
                        writeWavHeader(f, uint32_t(channels), rate, uint64_t(written) * channels * 3);
                    }
                    if (fclose(f) != 0) rc = fail("error writing WAV", 6);
                    else if (written == 0) rc = fail("audio decode failed", 7);
                    else {
                        if (written != totalSamples)
                            fprintf(stderr, "kickoff_r3d: warning: decoded %llu of %llu samples\n", written, totalSamples);
                        const double duration = fps > 0 ? double(frames) / fps : double(written) / rate;
                        printf("{\"fps\":%.6g,\"width\":%zu,\"height\":%zu,\"frames\":%zu,\"duration\":%.6f,"
                               "\"channels\":%zu,\"rate\":%u,\"audio_duration\":%.6f,\"timecode\":\"%s\"}\n",
                               fps, clip.Width(), clip.Height(), frames, duration, channels, rate,
                               double(written) / rate, jsonEscape(timecode).c_str());
                    }
                }
            }
        }
    }
    FinalizeSdk();
    return rc;
}
