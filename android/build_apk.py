"""Build the small native shell with official Android SDK tools, without Gradle.
Example: python android/build_apk.py --platform <android-35> --tools <build-tools/34.0.0> --java-bin <jdk/bin>
The default debug key is for private testing only. Keep it to install future updates.

Pass ``--dev-local`` ONLY for developer builds that may connect to a USB
localhost service; the standard (release) APK is hardwired to the fixed
public origin and exposes no address input.
"""
import argparse
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--platform',required=True)
    p.add_argument('--tools',required=True)
    p.add_argument('--java-bin',required=True)
    p.add_argument('--build-dir',default=str(ROOT/'build'))
    p.add_argument('--out',help='output APK; defaults to artifacts/beta, never overwrites production')
    p.add_argument('--origin',default='https://your-domain.example:28443',
                   help='Fixed origin server URL for release build (e.g. https://your-domain.com:28443)')
    p.add_argument('--dev-local',action='store_true',
                   help='dev-only: allow address input (USB localhost testing); '
                        'omitted for the fixed-origin release build')
    a=p.parse_args()
    platform,tools,jbin,build=map(lambda s:Path(s).resolve(),(a.platform,a.tools,a.java_bin,a.build_dir))
    out=Path(a.out).resolve() if a.out else ROOT.parent/'artifacts'/'beta'/('together-beta.apk' if a.dev_local else 'together-release-candidate.apk')
    out.parent.mkdir(parents=True,exist_ok=True)
    suffix='.exe' if os.name=='nt' else ''
    build.mkdir(parents=True,exist_ok=True)
    classes=build/'classes';classes.mkdir(exist_ok=True)
    dex=build/'dex';dex.mkdir(exist_ok=True)
    def run(args):
        subprocess.run([str(x) for x in args],check=True)
    jar=platform/'android.jar'
    # Generated config compiled together with the sources.
    gen=build/'gen'/'app'/'together'/'reader'
    gen.mkdir(parents=True,exist_ok=True)
    (gen/'BuildConfig.java').write_text(
        'package app.together.reader;\n'
        'public final class BuildConfig {\n'
        '  public static final boolean ALLOW_LOCAL_DEBUG = %s;\n'
        '  public static final String FIXED_ORIGIN = "%s";\n'
        '}\n' % ('true' if a.dev_local else 'false', a.origin),
        encoding='utf-8')
    run([jbin/('javac'+suffix),'-encoding','UTF-8','--release','8','-classpath',jar,'-d',classes,
         *ROOT.glob('src/**/*.java'),*gen.glob('*.java')])
    run([jbin/('java'+suffix),'-cp',tools/'lib/d8.jar','com.android.tools.r8.D8','--lib',jar,'--min-api','26','--output',dex,*classes.rglob('*.class')])
    unsigned=build/'unsigned.apk'
    run([tools/('aapt'+suffix),'package','-f','-M',ROOT/'AndroidManifest.xml','-S',ROOT/'res','-I',jar,'-F',unsigned])
    with zipfile.ZipFile(unsigned,'a',zipfile.ZIP_DEFLATED) as z:
        for file in dex.glob('*.dex'):
            z.write(file,file.name)
    aligned=build/'aligned.apk'
    run([tools/('zipalign'+suffix),'-f','4',unsigned,aligned])
    key=build/'test-signing.jks'
    if not key.exists():
        run([jbin/('keytool'+suffix),'-genkeypair','-keystore',key,'-storepass','android','-keypass','android','-alias','together-test','-keyalg','RSA','-keysize','2048','-validity','3650','-dname','CN=Together Private Test,O=Personal,C=CN'])
    run([jbin/('java'+suffix),'-jar',tools/'lib/apksigner.jar','sign','--ks',key,'--ks-pass','pass:android','--key-pass','pass:android','--out',out,aligned])
    run([jbin/('java'+suffix),'-jar',tools/'lib/apksigner.jar','verify','--verbose',out])
    run([tools/('aapt'+suffix),'dump','badging',out])
    print('APK created:',out)

if __name__=='__main__':
    main()
