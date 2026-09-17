import AVFoundation
import SwiftUI
import UIKit

struct ShareSheetSetupView: View {
  @Environment(\.dismiss) private var dismiss
  @Environment(\.accessibilityReduceMotion) private var reduceMotion

  var body: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 20) {
          Image(systemName: "square.and.arrow.up")
            .font(.system(size: 32, weight: .semibold))
            .foregroundStyle(.orange)
            .frame(width: 64, height: 64)
            .background(Color.orange.opacity(0.12), in: Circle())
            .accessibilityHidden(true)

          VStack(spacing: 10) {
            Text("Keep Jot one tap away")
              .font(.title2.bold())

            Text("Add Jot to your Share Sheet Favorites so it’s easy to find whenever you save a place.")
              .foregroundStyle(.secondary)
          }
          .multilineTextAlignment(.center)

          LoopingShareSetupVideo(playsAutomatically: !reduceMotion)
            .aspectRatio(1206 / 1740, contentMode: .fit)
            .frame(maxWidth: 340)
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
            .overlay {
              RoundedRectangle(cornerRadius: 24, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.08))
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(
              "Video showing how to open More, tap Edit, add Jot to Favorites, and move it to the top"
            )

          Text("Tap More → Edit → + beside Jot. Move Jot to the top, then tap Done.")
            .font(.subheadline.weight(.medium))
            .multilineTextAlignment(.center)
            .frame(maxWidth: 360)
        }
        .padding(.horizontal, 24)
        .padding(.top, 20)
        .padding(.bottom, 112)
        .frame(maxWidth: .infinity)
      }
      .background(Color(uiColor: .systemGroupedBackground))
      .navigationTitle("Set up sharing")
      .navigationBarTitleDisplayMode(.inline)
      .toolbar {
        ToolbarItem(placement: .cancellationAction) {
          Button { dismiss() } label: {
            Image(systemName: "xmark")
          }
          .accessibilityLabel("Close")
        }
      }
      .safeAreaInset(edge: .bottom) {
        Button("Got it") { dismiss() }
          .font(.headline)
          .frame(maxWidth: 420, minHeight: 52)
          .background(Color.orange, in: Capsule())
          .foregroundStyle(.white)
          .padding(.horizontal, 24)
          .padding(.vertical, 12)
          .frame(maxWidth: .infinity)
          .background(.ultraThinMaterial)
      }
    }
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
