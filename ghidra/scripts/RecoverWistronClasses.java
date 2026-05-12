// Recover C++ class layouts + vtable dispatch typing for Wistron's
// monitor firmware update binaries.
//
// Why this exists: Ghidra's built-in RecoverClassesFromRTTIScript handles
// Windows binaries reasonably well but its GCC class recovery is labeled
// "early stages of development" in its own docstring and SKIPS classes
// with virtual inheritance. Most of Wistron's chip-driver classes
// (IIC_INTF / HID_INTF base ⇄ RTS5409S_HID / FL5500_IIC_API / … overrides)
// use virtual inheritance, so the built-in script leaves them as opaque
// `*(code **)(lVar5 + 0x120)` indirect dispatches.
//
// The Ghidra-Cpp-Class-Analyzer extension addressed this but its upstream
// was archived in Oct 2023 and the only active fork (Fancy2209) hasn't
// completed the port to Ghidra 12 (30+ API-mismatch compile errors).
//
// So we do it ourselves, taking advantage of the fact that we only need
// to type a small, well-known set of classes per binary — the chip-driver
// hierarchies — not every class in the binary. For each class listed in
// the descriptor:
//   1. Look up the GCC typeinfo (`typeinfo for <ClassName>`) and the
//      vtable (`vtable for <ClassName>`).
//   2. Create a `<ClassName>::vtable` struct with a function-pointer
//      field for every slot, named per the descriptor's `vtable` map.
//   3. Create a `<ClassName>` class struct with `__vftable` at offset 0
//      plus any known data members from the descriptor's `fields` list.
//   4. Set the `this` parameter type to `<ClassName> *` on every method
//      in the class's namespace.
//
// After running this, the decompiler resolves vtable calls through the
// typed `this` pointer, so the dispatch chain
//   (**(code **)(*plVar7 + 0x120))(plVar7, this[0x31], 0xf4, 0x9f)
// becomes
//   this->iic->vtable->write_byte(this->iic, this->i2c_slave, 0xF4, 0x9F)
// which is dramatically easier to read.
//
// To extend to a new binary:
//   - Add `ghidra/scripts/wistron-classes/<binary-name>.json` next to
//     this script (e.g. `libdevices.so.json`, `firmware-updater.json`).
//   - The script auto-loads the descriptor for `currentProgram.getName()`.
//   - Re-run `nix run .#recover-classes -- <binary>` then `decompile`.
//
//@category C++/Wistron

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.data.AbstractIntegerDataType;
import ghidra.program.model.data.CategoryPath;
import ghidra.program.model.data.DataType;
import ghidra.program.model.data.DataTypeConflictHandler;
import ghidra.program.model.data.DataTypeManager;
import ghidra.program.model.data.FunctionDefinitionDataType;
import ghidra.program.model.data.ParameterDefinition;
import ghidra.program.model.data.ParameterDefinitionImpl;
import ghidra.program.model.data.PointerDataType;
import ghidra.program.model.data.Structure;
import ghidra.program.model.data.StructureDataType;
import ghidra.program.model.data.Undefined1DataType;
import ghidra.program.model.data.Undefined4DataType;
import ghidra.program.model.data.Undefined8DataType;
import ghidra.program.model.data.VoidDataType;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Parameter;
import ghidra.program.model.listing.ParameterImpl;
import ghidra.program.model.symbol.Namespace;
import ghidra.program.model.symbol.SourceType;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolTable;

public class RecoverWistronClasses extends GhidraScript {

	private static final CategoryPath CATEGORY = new CategoryPath("/Wistron");

	private DataTypeManager dtm;
	private SymbolTable symbolTable;
	private FunctionManager functionManager;
	private int pointerSize;

	@Override
	public void run() throws Exception {
		dtm = currentProgram.getDataTypeManager();
		symbolTable = currentProgram.getSymbolTable();
		functionManager = currentProgram.getFunctionManager();
		pointerSize = currentProgram.getDefaultPointerSize();

		String binaryName = currentProgram.getName();
		File descriptor = findDescriptor(binaryName);
		if (descriptor == null) {
			println("No descriptor found for binary '" + binaryName + "'.");
			println("Expected a file at " + descriptorSearchHint(binaryName));
			println("Add one to define the class hierarchy for this binary.");
			return;
		}
		println("Loading descriptor: " + descriptor.getAbsolutePath());

		JsonObject root;
		try {
			String json = Files.readString(descriptor.toPath());
			root = JsonParser.parseString(json).getAsJsonObject();
		}
		catch (IOException e) {
			printerr("failed to read descriptor: " + e.getMessage());
			return;
		}

		// First pass: create class + vtable structs at their final size,
		// so cross-references (e.g. REALTEK_API.iic_intf points to
		// IIC_INTF*) resolve in the second pass regardless of declaration
		// order. We size each struct from the descriptor up front: the
		// vtable's size is `max(slot offsets) + ptr size`, and the class
		// struct's size comes from its declared `size`. Structures with
		// length 0 in Ghidra get treated as length 1, which trips up
		// `replaceAtOffset` at the very last slot — so we create them
		// pre-sized.
		JsonArray classes = root.getAsJsonArray("classes");
		Map<String, Structure> classStructs = new HashMap<>();
		Map<String, Structure> vtableStructs = new HashMap<>();
		for (JsonElement el : classes) {
			JsonObject cls = el.getAsJsonObject();
			String name = cls.get("name").getAsString();
			classStructs.put(name, createEmptyClassStruct(name, cls));
			vtableStructs.put(name, createEmptyVtableStruct(name, cls));
		}

		int applied = 0;
		int skipped = 0;
		for (JsonElement el : classes) {
			JsonObject cls = el.getAsJsonObject();
			String name = cls.get("name").getAsString();
			try {
				populateClass(cls, classStructs, vtableStructs);
				int methods = applyToMethods(name, classStructs.get(name));
				println(String.format("  %s: %d methods typed", name, methods));
				applied += methods;
			}
			catch (Exception e) {
				printerr("class " + name + ": " + e.getMessage());
				e.printStackTrace();
				skipped++;
			}
		}
		println(String.format("Done. %d methods typed across %d classes (%d skipped).",
			applied, classes.size() - skipped, skipped));
	}

	/**
	 * Look for `wistron-classes/<binary>.json` next to this script. The
	 * descriptor matches Ghidra's program name exactly (e.g.
	 * "libdisplay.so", "Firmware Updater").
	 */
	private File findDescriptor(String binaryName) {
		File scriptDir = getSourceFile().getParentFile().getFile(true);
		File descDir = new File(scriptDir, "wistron-classes");
		File desc = new File(descDir, binaryName + ".json");
		return desc.exists() ? desc : null;
	}

	private String descriptorSearchHint(String binaryName) {
		File scriptDir = getSourceFile().getParentFile().getFile(true);
		return new File(scriptDir, "wistron-classes/" + binaryName + ".json")
			.getAbsolutePath();
	}

	/**
	 * Create an empty class struct. If one exists from a previous run we
	 * drop it first — the field layouts may have changed and replaying
	 * into a stale struct triggers "not enough undefined bytes" errors
	 * when slot sizes shift. The struct's actual size is set in
	 * {@link #populateClass}.
	 */
	private Structure createEmptyClassStruct(String name, JsonObject cls) {
		DataType existing = dtm.getDataType(CATEGORY, name);
		if (existing != null) {
			dtm.remove(existing, monitor);
		}
		int size = parseHexOrInt(cls, "size", 0);
		Structure s = new StructureDataType(CATEGORY, name, size, dtm);
		return (Structure) dtm.addDataType(s, DataTypeConflictHandler.REPLACE_HANDLER);
	}

	private Structure createEmptyVtableStruct(String name, JsonObject cls) {
		String vtName = name + "::vtable";
		DataType existing = dtm.getDataType(CATEGORY, vtName);
		if (existing != null) {
			dtm.remove(existing, monitor);
		}
		// Size = max slot offset + 1 pointer. Pre-sizing avoids the
		// "0-length structures report as 1 byte" gotcha that would
		// otherwise underflow `replaceAtOffset` at the last slot.
		int maxOffset = 0;
		if (cls.has("vtable")) {
			for (Map.Entry<String, JsonElement> e : cls.getAsJsonObject("vtable").entrySet()) {
				if (e.getKey().startsWith("//")) continue;
				int off = parseHexOrInt(e.getKey());
				if (off > maxOffset) maxOffset = off;
			}
		}
		int size = maxOffset + pointerSize;
		Structure s = new StructureDataType(CATEGORY, vtName, size, dtm);
		return (Structure) dtm.addDataType(s, DataTypeConflictHandler.REPLACE_HANDLER);
	}

	private void populateClass(JsonObject cls,
		Map<String, Structure> classStructs,
		Map<String, Structure> vtableStructs) throws Exception {

		String name = cls.get("name").getAsString();
		Structure classStruct = classStructs.get(name);
		Structure vtableStruct = vtableStructs.get(name);

		// 1. Populate the vtable struct. Slot 0 of every C++ vtable struct
		// is the offset-to-top (or RTTI ptr, depending on slot 0 vs 1 — GCC
		// puts offset-to-top at -0x10, RTTI at -0x08 relative to the vptr,
		// so the vtable AS REFERENCED by the vptr starts at slot 0 = first
		// virtual method). All slots are pointer-sized.
		JsonObject vtableDesc = cls.has("vtable")
			? cls.getAsJsonObject("vtable") : new JsonObject();

		// Walk every entry in the descriptor and append a pointer-typed
		// field at the requested byte offset. The descriptor's offsets are
		// the BYTE offsets we observe in the decomp, not slot indices.
		// Skip "//" keys — they're JSON-comment conventions.
		List<Map.Entry<Integer, String>> sorted = new ArrayList<>();
		for (Map.Entry<String, JsonElement> e : vtableDesc.entrySet()) {
			if (e.getKey().startsWith("//")) continue;
			int off = parseHexOrInt(e.getKey());
			JsonObject slot = e.getValue().getAsJsonObject();
			String methodName = slot.get("name").getAsString();
			sorted.add(Map.entry(off, methodName));
		}
		sorted.sort(Map.Entry.comparingByKey());

		// Each named slot becomes a `void *<method>` field. We use a void*
		// (rather than a FunctionDefinitionDataType) because cross-class
		// method signatures vary and pinning them down would just confuse
		// the decompiler without much win. The slot NAME is what makes the
		// decomp readable.
		DataType voidPtr = new PointerDataType(VoidDataType.dataType, pointerSize, dtm);
		for (Map.Entry<Integer, String> entry : sorted) {
			int off = entry.getKey();
			String method = entry.getValue();
			try {
				vtableStruct.replaceAtOffset(off, voidPtr, pointerSize, method, "");
			}
			catch (IllegalArgumentException ex) {
				printerr("  vtable " + name + " slot 0x" + Integer.toHexString(off) +
					" (" + method + "): " + ex.getMessage());
			}
		}

		// 2. Populate the class struct. First field at offset 0 is the
		// vtable pointer; subsequent fields come from the descriptor.
		PointerDataType vptr = new PointerDataType(vtableStruct, pointerSize, dtm);
		try {
			classStruct.replaceAtOffset(0, vptr, pointerSize, "__vftable", "");
		}
		catch (IllegalArgumentException ex) {
			printerr("  class " + name + " __vftable: " + ex.getMessage());
		}

		if (cls.has("fields")) {
			for (JsonElement el : cls.getAsJsonArray("fields")) {
				JsonObject field = el.getAsJsonObject();
				int off = parseHexOrInt(field, "offset", -1);
				if (off < 0) {
					printerr("  class " + name + ": field missing 'offset'");
					continue;
				}
				String fname = field.get("name").getAsString();
				String typeStr = field.has("type") ? field.get("type").getAsString() : "u8";
				DataType dt = resolveFieldType(typeStr, classStructs, vtableStructs);
				if (dt == null) {
					printerr("  class " + name + " field " + fname +
						": unknown type '" + typeStr + "'");
					continue;
				}
				try {
					classStruct.replaceAtOffset(off, dt, dt.getLength(), fname, "");
				}
				catch (IllegalArgumentException ex) {
					printerr("  class " + name + " field " + fname +
						" @ 0x" + Integer.toHexString(off) + ": " + ex.getMessage());
				}
			}
		}
	}

	/**
	 * Find every function in the class's namespace and set its first
	 * (`this`) parameter type to `<ClassName> *`. Skips destructors which
	 * Ghidra tends to track separately.
	 */
	private int applyToMethods(String className, Structure classStruct) throws Exception {
		Namespace ns = findNamespace(className);
		if (ns == null) {
			println("  " + className + ": no namespace found in symbol table");
			return 0;
		}
		PointerDataType thisType = new PointerDataType(classStruct, pointerSize, dtm);

		int count = 0;
		SymbolIterator iter = symbolTable.getSymbols(ns);
		while (iter.hasNext()) {
			Symbol sym = iter.next();
			Function f = functionManager.getFunctionAt(sym.getAddress());
			if (f == null) {
				continue;
			}
			Parameter[] params = f.getParameters();
			if (params.length == 0 || !"this".equals(params[0].getName())) {
				// Ghidra typically auto-names the first param `this` for
				// __thiscall functions. If it didn't here, we don't have a
				// safe way to identify the this-param.
				continue;
			}
			try {
				// __thiscall's `this` is an auto-parameter (Ghidra
				// reserves register storage based on the calling
				// convention). Auto-parameters can't be directly retyped;
				// flipping the function to "custom variable storage" lets
				// us own the parameter list. After the change Ghidra
				// stops auto-assigning storage for this function, but the
				// register we'd pick is the same one __thiscall already
				// used, so the storage stays effectively identical.
				if (!f.hasCustomVariableStorage()) {
					f.setCustomVariableStorage(true);
				}
				// After setCustomVariableStorage the parameter array may
				// have been rebuilt; re-fetch.
				params = f.getParameters();
				if (params.length > 0 && "this".equals(params[0].getName())) {
					params[0].setDataType(thisType, SourceType.USER_DEFINED);
					count++;
				}
			}
			catch (Exception ex) {
				printerr("    " + f.getName() + " @ " + f.getEntryPoint() +
					": " + ex.getMessage());
			}
		}
		return count;
	}

	/**
	 * Find a Ghidra namespace by C++ class name. Walks all namespaces
	 * because C++ classes can be nested (e.g. `CryptoPP::SHA3`); we match
	 * the leaf name.
	 */
	private Namespace findNamespace(String className) {
		// Try direct lookup at global first.
		Namespace global = currentProgram.getGlobalNamespace();
		Namespace ns = symbolTable.getNamespace(className, global);
		if (ns != null) {
			return ns;
		}
		// Fall back to scanning every namespace symbol. Classes nested in
		// namespaces (CryptoPP::, std::) show up that way.
		for (Symbol sym : symbolTable.getSymbolIterator(className, true)) {
			Namespace candidate = sym.getParentNamespace();
			Namespace own = symbolTable.getNamespace(className, candidate);
			if (own != null) {
				return own;
			}
			if (sym.getObject() instanceof Namespace) {
				return (Namespace) sym.getObject();
			}
		}
		return null;
	}

	/**
	 * Resolve a field type string from the descriptor. Supports primitives
	 * (u8/u16/u32/u64, s8/s16/s32/s64, void*), pointer-to-known-class
	 * (ClassName*), and undefined-with-size (undef4, undef8, raw bytes).
	 */
	private DataType resolveFieldType(String type, Map<String, Structure> classStructs,
		Map<String, Structure> vtableStructs) {
		type = type.trim();
		if (type.endsWith("*")) {
			String base = type.substring(0, type.length() - 1).trim();
			DataType target;
			if (classStructs.containsKey(base)) {
				target = classStructs.get(base);
			}
			else if (vtableStructs.containsKey(base)) {
				target = vtableStructs.get(base);
			}
			else if ("void".equals(base)) {
				target = VoidDataType.dataType;
			}
			else {
				return null;
			}
			return new PointerDataType(target, pointerSize, dtm);
		}
		switch (type) {
			case "u8": case "uint8": case "byte":
				return AbstractIntegerDataType.getUnsignedDataType(1, dtm);
			case "u16": case "uint16":
				return AbstractIntegerDataType.getUnsignedDataType(2, dtm);
			case "u32": case "uint32":
				return AbstractIntegerDataType.getUnsignedDataType(4, dtm);
			case "u64": case "uint64":
				return AbstractIntegerDataType.getUnsignedDataType(8, dtm);
			case "s8": case "int8":
				return AbstractIntegerDataType.getSignedDataType(1, dtm);
			case "s16": case "int16":
				return AbstractIntegerDataType.getSignedDataType(2, dtm);
			case "s32": case "int32":
				return AbstractIntegerDataType.getSignedDataType(4, dtm);
			case "s64": case "int64":
				return AbstractIntegerDataType.getSignedDataType(8, dtm);
			case "undef1":
				return Undefined1DataType.dataType;
			case "undef4":
				return Undefined4DataType.dataType;
			case "undef8":
				return Undefined8DataType.dataType;
		}
		return null;
	}

	private int parseHexOrInt(JsonObject obj, String key, int def) {
		if (!obj.has(key)) return def;
		return parseHexOrInt(obj.get(key).getAsString());
	}

	private int parseHexOrInt(String s) {
		s = s.trim();
		if (s.startsWith("0x") || s.startsWith("0X")) {
			return Integer.parseInt(s.substring(2), 16);
		}
		return Integer.parseInt(s);
	}
}
