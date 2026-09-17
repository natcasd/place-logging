import AVFoundation
import SwiftUI
import UIKit

struct ShareSheetSetupView: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(\.accessibilityReduceMotion) private var reduceMotion

  var body: some View {
    VStack(spacing: 14) {
      HStack(spacing: 12) {
        Text("Add Jot to Favorites")
          .font(.title2.bold())

        Spacer()

        Button { dismiss() } label: {
          Image(systemName: "xmark")
            .font(.body.weight(.semibold))
            .frame(width: 36, height: 36)
            .background(Color.primary.opacity(0.07), in: Circle())
        }
        .foregroundStyle(.primary)
        .accessibilityLabel("Close")
      }

      LoopingShareSetupVideo(playsAutomatically: !reduceMotion)
        .aspectRatio(1206 / 1740, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(
          "Video showing how to open More, tap Edit, add Jot to Favorites, and move it to the top"
        )

      Text("Tap More → Edit → + beside Jot. Move it to the top, then tap Done.")
        .font(.subheadline.weight(.medium))
        .multilineTextAlignment(.center)
        .fixedSize(horizontal: false, vertical: true)

      Button("Got it") { dismiss() }
        .font(.headline)
        .frame(maxWidth: .infinity, minHeight: 52)
        .background(Color.orange, in: Capsule())
        .foregroundStyle(.white)
    }
    .padding(.horizontal, 20)
    .padding(.top, 12)
    .padding(.bottom, 16)
    .frame(maxWidth: .infinity, maxHeight: .infinity)
    .background(Color(uiColor: .systemGroupedBackground))
    .presentationDragIndicator(.visible)
  }
}

private struct LoopingShareSetupVideo: UIViewRepresentable {
  let playsAutomatically: Bool

  func makeCoordinator() -> Coordinator {
    Coordinator(url: Bundle.main.url(forResource: "JotShareSetup", withExtension: "mp4"))
  }

  func makeUIView(context: Context) -> PlayerView {
    let view = PlayerView()
    view.playerLayer.player = context.coordinator.player
    updatePlayback(context.coordinator.player)
    return view
  }

  func updateUIView(_ uiView: PlayerView, context: Context) {
    updatePlayback(context.coordinator.player)
  }

  static func dismantleUIView(_ uiView: PlayerView, coordinator: Coordinator) {
    coordinator.player.pause()
  }

  private func updatePlayback(_ player: AVQueuePlayer) {
    if playsAutomatically {
      player.play()
    } else {
      player.pause()
      player.seek(to: .zero)
    }
  }

  final class Coordinator {
    let player = AVQueuePlayer()
    private var looper: AVPlayerLooper?

    init(url: URL?) {
      player.isMuted = true
      player.actionAtItemEnd = .none
      guard let url else { return }
      looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
    }
  }

  final class PlayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }

    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }

    override init(frame: CGRect) {
      super.init(frame: frame)
      playerLayer.videoGravity = .resizeAspectFill
      backgroundColor = .secondarySystemGroupedBackground
    }

    required init?(coder: NSCoder) {
      fatalError("init(coder:) has not been implemented")
    }
  }
}
