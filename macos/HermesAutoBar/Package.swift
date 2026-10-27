// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "HermesAutoBar",
    platforms: [.macOS("26.0")],
    products: [
        .executable(name: "hermes-auto-bar", targets: ["HermesAutoBar"]),
    ],
    targets: [
        // Pure logic: decoding the CLI's JSON, what needs attention, labels, grouping. Unit tested.
        .target(name: "HermesAutoCore"),
        // Thin SwiftUI/AppKit layer: menu bar item, window, CLI process.
        .executableTarget(name: "HermesAutoBar", dependencies: ["HermesAutoCore"]),
        .testTarget(name: "HermesAutoCoreTests", dependencies: ["HermesAutoCore"],
                    resources: [.copy("Fixtures")]),
    ]
)
