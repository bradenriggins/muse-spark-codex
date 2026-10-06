import AppKit
import SwiftUI

struct Mode: Identifiable, Hashable, Sendable {
    let id: String
    let title: String
}

let MODES = [
    Mode(id: "native", title: "Personal (GPT)"),
    Mode(id: "muse", title: "Muse Spark 1.3"),
    Mode(id: "muse-contributor", title: "Muse Contributor"),
    Mode(id: "muse-fast", title: "Muse Fast"),
    Mode(id: "muse-lean", title: "Muse Lean"),
    Mode(id: "muse-lean-contributor", title: "Muse Lean Contributor"),
]

func scriptURL() -> URL {
    FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".codex/muse-shim/muse-desktop")
}

func runScript(_ args: [String]) -> (Int32, String) {
    let task = Process()
    task.executableURL = scriptURL()
    task.arguments = args
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = pipe
    do {
        try task.run()
    } catch {
        return (1, "launch failed: \(error)")
    }
    task.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (task.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func currentMode() -> String {
    let (code, out) = runScript(["status"])
    guard code == 0 else { return "native" }
    for line in out.split(separator: "\n") {
        if line.hasPrefix("mode:") {
            return String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
        }
    }
    return "native"
}

@main
struct MuseBarApp: App {
    var body: some Scene {
        MenuBarExtra("Muse", systemImage: "bolt.fill") {
            MenuView()
        }
        .menuBarExtraStyle(.menu)
    }
}

struct MenuView: View {
    @State private var mode = "..."
    @State private var busy: String?

    var body: some View {
        Text("Codex mode: \(busy.map { "switching to \($0)..." } ?? modeName(mode))")
            .foregroundStyle(.secondary)
        Divider()
        ForEach(MODES) { m in
            Button {
                switchTo(m)
            } label: {
                Text((m.id == mode ? "✓ " : "") + m.title)
            }
            .disabled(busy != nil || m.id == mode)
        }
        Divider()
        Button("Restart ChatGPT") {
            busy = "restart"
            Task.detached {
                _ = runScript(["restart"])
                await MainActor.run { busy = nil }
            }
        }
        .disabled(busy != nil)
        Button("Quit MuseBar") {
            NSApplication.shared.terminate(nil)
        }
        .onAppear { refresh() }
    }

    func modeName(_ id: String) -> String {
        MODES.first { $0.id == id }?.title ?? id
    }

    func refresh() {
        mode = currentMode()
    }

    func switchTo(_ m: Mode) {
        busy = m.title
        Task.detached {
            let (code, _) = runScript(["mode", m.id])
            await MainActor.run {
                busy = nil
                if code == 0 { refresh() }
            }
        }
    }
}
