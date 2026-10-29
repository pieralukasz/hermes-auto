import SwiftUI

extension Color {
    // Native secondary labels are too faint on the light menu surface.
    static let secondaryText = Color.primary.opacity(0.75)
}
