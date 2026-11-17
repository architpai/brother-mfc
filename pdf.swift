import Foundation
import CoreGraphics
import ImageIO
import PDFKit

struct Assembly: Decodable {
    let files: [String]
    let expected_pages: Int?
    let rotation: Int
}

enum PDFError: Error, CustomStringConvertible {
    case invalid(String)
    var description: String {
        switch self {
        case .invalid(let message): return message
        }
    }
}

func load(_ path: String) throws -> PDFDocument {
    guard let document = PDFDocument(url: URL(fileURLWithPath: path)), document.pageCount > 0 else {
        throw PDFError.invalid("Cannot read PDF: \(path)")
    }
    return document
}

func save(_ document: PDFDocument, _ path: String) throws {
    guard document.pageCount > 0 else { throw PDFError.invalid("No pages were captured") }
    let rotations = (0..<document.pageCount).map { document.page(at: $0)!.rotation }
    guard document.write(to: URL(fileURLWithPath: path)) else {
        throw PDFError.invalid("Cannot save PDF: \(path)")
    }
    let saved = try load(path)
    guard saved.pageCount == document.pageCount,
          (0..<saved.pageCount).allSatisfy({ saved.page(at: $0)?.rotation == rotations[$0] }) else {
        throw PDFError.invalid("Saved PDF page count or rotation did not match")
    }
    FileHandle.standardError.write(Data("Verified \(saved.pageCount) PDF pages\n".utf8))
}

func imagePDF(_ path: String, _ output: URL) throws -> PDFDocument {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any] else {
        throw PDFError.invalid("Cannot read scanned image: \(path)")
    }
    let dpiX = (properties[kCGImagePropertyDPIWidth] as? NSNumber)?.doubleValue ?? 72
    let dpiY = (properties[kCGImagePropertyDPIHeight] as? NSNumber)?.doubleValue ?? 72
    guard dpiX > 0, dpiY > 0 else { throw PDFError.invalid("Invalid image density: \(path)") }
    var box = CGRect(x: 0, y: 0, width: Double(image.width) * 72 / dpiX,
                     height: Double(image.height) * 72 / dpiY)
    guard let consumer = CGDataConsumer(url: output as CFURL),
          let context = CGContext(consumer: consumer, mediaBox: &box, nil) else {
        throw PDFError.invalid("Cannot create PDF for scanned image")
    }
    context.beginPDFPage(nil)
    context.draw(image, in: box)
    context.endPDFPage()
    context.closePDF()
    return try load(output.path)
}

func assemble(_ manifest: String, _ output: String) throws {
    let options = try JSONDecoder().decode(Assembly.self, from: Data(contentsOf: URL(fileURLWithPath: manifest)))
    let temporary = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: false,
                                          attributes: [.posixPermissions: 0o700])
    defer { try? FileManager.default.removeItem(at: temporary) }
    let merged = PDFDocument()
    for (fileIndex, path) in options.files.enumerated() {
        let source: PDFDocument
        if path.lowercased().hasSuffix(".jpg") {
            source = try imagePDF(path, temporary.appendingPathComponent("\(fileIndex).pdf"))
        } else {
            source = try load(path)
        }
        for index in 0..<source.pageCount {
            guard let page = source.page(at: index) else { throw PDFError.invalid("Cannot read PDF page") }
            page.rotation = (page.rotation + options.rotation) % 360
            merged.insert(page, at: merged.pageCount)
        }
    }
    if let expected = options.expected_pages, merged.pageCount != expected {
        throw PDFError.invalid("Expected \(expected) pages, captured \(merged.pageCount). Original files are retained.")
    }
    try save(merged, output)
}

do {
    let arguments = Array(CommandLine.arguments.dropFirst())
    switch arguments.first {
    case "check":
        break
    case "assemble" where arguments.count == 3:
        try assemble(arguments[1], arguments[2])
    case "rotate" where arguments.count == 4:
        guard let degrees = Int(arguments[2]), [0, 90, 180, 270].contains(degrees) else {
            throw PDFError.invalid("Invalid rotation")
        }
        let document = try load(arguments[1])
        for index in 0..<document.pageCount {
            guard let page = document.page(at: index) else { throw PDFError.invalid("Cannot read PDF page") }
            page.rotation = (page.rotation + degrees) % 360
        }
        try save(document, arguments[3])
    default:
        throw PDFError.invalid("Use brother-mfc --help")
    }
} catch {
    FileHandle.standardError.write(Data("PDF error: \(error)\n".utf8))
    exit(1)
}
