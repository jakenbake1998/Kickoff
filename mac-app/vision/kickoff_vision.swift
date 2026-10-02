// kickoff_vision: looks at frames of a video with Apple Vision (built into macOS, runs on the Mac)
// for the Slop Cut. Usage: kickoff_vision FILE START END STEP (seconds of the file). Prints one
// JSON line per frame: t, the labels Vision sees that matter for a band (instruments, mics, stage),
// how many people and faces, the tallest person and face as a share of the frame height, and
// where the tallest person stands (0 left .. 1 right). A frame that can't be read prints "error".
// Built by kickoff-update.sh: xcrun swiftc -O -o kickoff_vision kickoff_vision.swift
import AVFoundation
import Foundation
import Vision

let args = CommandLine.arguments
guard args.count >= 5, let start = Double(args[2]), let end = Double(args[3]), let step = Double(args[4]), step > 0 else {
    FileHandle.standardError.write("usage: kickoff_vision FILE START END STEP\n".data(using: .utf8)!)
    exit(2)
}
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let gen = AVAssetImageGenerator(asset: asset)
gen.appliesPreferredTrackTransform = true
gen.maximumSize = CGSize(width: 640, height: 640)
let tol = CMTime(seconds: min(0.5, step / 2), preferredTimescale: 600)
gen.requestedTimeToleranceBefore = tol
gen.requestedTimeToleranceAfter = tol

let keep = ["guitar", "bass", "banjo", "ukulele", "drum", "cymbal", "percussion", "microphone", "singer",
            "karaoke", "piano", "keyboard", "organ", "synthesizer", "accordion", "musical_instrument",
            "concert", "stage", "crowd", "band", "people", "music"]

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func r3(_ x: Double) -> Double { (x * 1000).rounded() / 1000 }

var t = start
while t <= end + 1e-6 {
    var line: [String: Any] = ["t": r3(t)]
    if let img = try? gen.copyCGImage(at: CMTime(seconds: t, preferredTimescale: 600), actualTime: nil) {
        let handler = VNImageRequestHandler(cgImage: img, options: [:])
        let classify = VNClassifyImageRequest()
        let humans = VNDetectHumanRectanglesRequest()
        let faces = VNDetectFaceRectanglesRequest()
        try? handler.perform([classify, humans, faces])
        var labels: [String: Double] = [:]
        for o in classify.results ?? [] where o.confidence > 0.05 {
            let id = o.identifier.lowercased()
            if keep.contains(where: { id.contains($0) }) { labels[o.identifier] = r3(Double(o.confidence)) }
        }
        let people = humans.results ?? []
        let found = faces.results ?? []
        line["labels"] = labels
        line["people"] = people.count
        line["faces"] = found.count
        line["big"] = r3(people.map { Double($0.boundingBox.height) }.max() ?? 0)
        line["face"] = r3(found.map { Double($0.boundingBox.height) }.max() ?? 0)
        if let p = people.max(by: { $0.boundingBox.height < $1.boundingBox.height }) {
            line["x"] = r3(Double(p.boundingBox.midX))
        }
    } else {
        line["error"] = "no frame"
    }
    emit(line)
    t += step
}
