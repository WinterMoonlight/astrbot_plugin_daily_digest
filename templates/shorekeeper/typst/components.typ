// Static Typst preview. 1 pt is exported as 2 pixels at 144 PPI.
#let data = json(bytes(sys.inputs.at("data")))
#let body-font = data.fonts.body
#let display-font = data.fonts.display
#let latin-font = data.fonts.latin
#let ink = rgb("355c76")
#let body-ink = rgb("315674")
#let accents = (rgb("5d9fc8"), rgb("8596c5"), rgb("5ca6ad"))
#let tints = (rgb("eff8fc"), rgb("f4f5fc"), rgb("f1f9f9"))
#let spark(size: 9pt, paint: rgb("83b3cb")) = polygon(
  (size/2, 0pt), (size*0.63, size*0.37), (size, size/2),
  (size*0.63, size*0.63), (size/2, size), (size*0.37, size*0.63),
  (0pt, size/2), (size*0.37, size*0.37), fill: none, stroke: 0.5pt + paint,
)
#let plain(value) = value.split("\n").map(line => text(line)).join(linebreak())

#let header(data) = block(
  width: 100%, height: 110pt, radius: 15pt, clip: true,
  inset: (x: 16pt, y: 11pt), stroke: 0.5pt + rgb("ffffffbb"),
  fill: gradient.linear(rgb("183b64"), rgb("33759b"), rgb("7dc6df"), angle: 12deg),
)[
  #place(bottom + right, dx: 18pt, dy: 12pt, text(font: latin-font, size: 39pt, weight: "bold", fill: rgb("ffffff10"))[SHOREKEEPER])
  #grid(columns: (1fr, auto), align: (left, right), column-gutter: 8pt,
    [
      #grid(columns: (9pt, 1fr), column-gutter: 8pt, align: horizon, spark(size: 9pt, paint: rgb("d2eef8")), text(size: 7.5pt, tracking: 1.5pt, fill: rgb("d2eef8"))[SHOREKEEPER])
      #v(8pt)
      #text(size: 29pt, weight: "bold", tracking: 2pt, fill: white)[#data.title]
    ],
    [
      #block(radius: 7.5pt, inset: (x: 10pt, y: 6.5pt), stroke: 0.5pt + rgb("ffffff55"), fill: rgb("ffffff18"))[
        #text(font: latin-font, size: 13.5pt, fill: white)[#data.date]
      ]
      #v(4pt)
      #text(size: 9pt, fill: rgb("e3f3fb"))[守岸人 · 蓝白主题]
    ]
  )
  #v(11pt)
  #text(size: 12pt, fill: rgb("f4fbff"))[#plain(data.intro)]
]

#let card(item, index) = block(
  width: 100%, radius: 11pt, inset: (top: 9pt, bottom: 10pt, x: 10pt),
  stroke: (left: 1.5pt + accents.at(calc.rem(index, 3)), rest: 0.5pt + white),
  fill: gradient.linear(white, tints.at(calc.rem(index, 3)), angle: 10deg),
)[
  #grid(columns: (1fr, 32pt), column-gutter: 10pt, align: horizon,
    [
      #box(radius: 3pt, inset: (x: 3pt, y: 1pt), fill: accents.at(calc.rem(index, 3)))[#text(size: 7.5pt, weight: "bold", fill: white)[#if index < 9 { "0" + str(index + 1) } else { str(index + 1) }]]
      #h(4pt)#text(size: 9pt, fill: accents.at(calc.rem(index, 3)))[#item.label]
      #v(6pt)
      #text(size: 14pt, weight: "bold", fill: rgb("17394f"))[#item.title]
    ],
    image(item.sticker, width: 32pt, height: 32pt, fit: "contain"),
  )
  #v(7.5pt)
  #block(width: 100%, radius: 8.5pt, inset: (x: 10pt, y: 10pt), stroke: 0.5pt + white,
      fill: gradient.linear(rgb("eff8ff"), rgb("eaf4fc"), angle: 90deg))[
    #set text(size: 12pt, fill: body-ink)
    #set par(leading: 9pt)
    #plain(item.body)
  ]
]

#let panel(data) = block(
  width: 100%, radius: 15pt, inset: 11pt, stroke: 0.5pt + white,
  fill: gradient.linear(rgb("dceff9"), rgb("e9effb"), rgb("e2f3f7"), angle: 35deg),
)[
  #grid(columns: (1fr, auto), align: horizon,
    block(radius: 8.5pt, inset: (x: 10pt, y: 6pt), fill: rgb("ffffffbd"), stroke: 0.5pt + white)[
      #text(size: 11.5pt, weight: "bold", fill: ink)[▤  值得留意的事]
    ],
    spark(size: 10pt),
  )
  #v(10pt)
  #for (index, item) in data.sections.enumerate() {
    if index > 0 { v(7pt) }
    card(item, index)
  }
]

#let closing(data) = block(
  width: 100%, height: 118pt, radius: 12pt, clip: true,
  fill: gradient.linear(rgb("f1f8fd"), rgb("f7faff"), angle: 0deg),
  stroke: (paint: rgb("bcd5e7"), thickness: 0.5pt, dash: "dashed"),
)[
  #place(top + right, dx: -12pt, dy: 9pt, spark(size: 9pt, paint: rgb("a8c6dc")))
  #grid(columns: (150pt, 1fr), column-gutter: 11pt, align: horizon,
    block(width: 150pt, height: 117pt, clip: true)[
      #place(top + left, dx: -65pt, dy: -8pt, image(data.assets.character, width: 225pt, height: 362.6pt, fit: "stretch"))
      #place(bottom + left, rect(width: 150pt, height: 22pt, stroke: none,
        fill: gradient.linear(rgb("f1f8fd00"), rgb("f1f8fd"), angle: 90deg)))
    ],
    pad(right: 19pt)[
      #text(font: display-font, size: 16pt, fill: rgb("557a9a"))[#data.closing.title]
      #v(9pt)
      #text(size: 10.5pt, fill: rgb("7e95aa"))[#data.closing.note]
      #v(8pt)
      #text(font: display-font, size: 10pt, fill: rgb("91a5b7"))[— 守岸人]
    ],
  )
]

#let end-rule() = grid(columns: (1fr, auto, 1fr), column-gutter: 7pt, align: horizon,
  line(length: 100%, stroke: 0.5pt + rgb("c3d9e8")),
  spark(size: 6pt, paint: rgb("71a2c3")),
  line(length: 100%, stroke: 0.5pt + rgb("c3d9e8")),
)
