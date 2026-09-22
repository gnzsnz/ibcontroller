# Java agent build -- plain javac + jar
#
# Expects JAVA_HOME to point to a JDK (set by SDKMAN locally, actions/setup-java in CI).
# --release targets Java 17. The agent runs under whatever JRE Gateway/TWS actually bundles
# so this targets the oldest confirmed version.

JDK ?= $(JAVA_HOME)
JAVAC := $(JDK)/bin/javac --release 17
JAR := $(JDK)/bin/jar

SRC := $(wildcard agent/src/main/java/ibcontroller/agent/*.java)

build/ibcontroller-agent.jar: $(SRC)
	mkdir -p build/classes
	$(JAVAC) -d build/classes $(SRC)
	$(JAR) cfe $@ ibcontroller.agent.AgentMain -C build/classes .
	cp build/ibcontroller-agent.jar ibcontroller/

.PHONY: build
build: build/ibcontroller-agent.jar

# The Python package can't be built standalone -- `ibcontroller/ibcontroller-agent.jar`
# must exist before `uv build` runs, since it's shipped via [tool.setuptools.package-data]
# the same way ibcontroller/data/*.json is (see pyproject.toml). This target makes that
# real dependency explicit instead of relying on a stale jar left over from a prior build.
.PHONY: dist
dist: build/ibcontroller-agent.jar
	uv build

.PHONY: clean
clean:
	rm -rf build dist
