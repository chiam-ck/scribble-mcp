import Foundation

// Read only: NSFileCoordinator asks File Provider to materialize the canonical
// file. The Python caller kills this process on a bounded hydration timeout.
guard CommandLine.arguments.count == 2 else {
    fputs("Usage: vault_coordinated_read <absolute-file-path>\n", stderr)
    exit(2)
}
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let coordinator = NSFileCoordinator(filePresenter: nil)
var coordinationError: NSError?
var readError: Error?
var content: Data?
coordinator.coordinate(readingItemAt: url, options: [.withoutChanges], error: &coordinationError) { coordinatedURL in
    do { content = try Data(contentsOf: coordinatedURL) }
    catch { readError = error }
}
if let error = coordinationError ?? readError as NSError? {
    fputs("\(error)\n", stderr)
    exit(1)
}
guard let data = content else {
    fputs("Coordinator completed without content\n", stderr)
    exit(1)
}
do { try FileHandle.standardOutput.write(contentsOf: data) }
catch {
    fputs("Output failed: \(error)\n", stderr)
    exit(1)
}
