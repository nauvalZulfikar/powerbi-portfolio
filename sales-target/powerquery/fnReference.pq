// One sheet of Reference.xlsx, headers promoted.
(sheet as text) as table =>
    let
        Book = Excel.Workbook(File.Contents(InboxFolder & "\reference\Reference.xlsx"), true, true)
    in
        Book{[Item = sheet, Kind = "Sheet"]}[Data]
